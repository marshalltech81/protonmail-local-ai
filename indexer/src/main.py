"""
Indexer entry point.
Watches the Maildir for new/changed emails, parses and threads them,
generates embeddings via an OpenAI-compatible /v1/embeddings endpoint,
and writes to the SQLite index.

The default embedder is the host-side mlx-service on Apple Metal, but
any OpenAI-compatible provider works (DeepInfra, OpenRouter, LM Studio,
vLLM, TEI, etc.) — only ``EMBED_BASE_URL`` + ``EMBED_MODEL`` (+ optional
``EMBED_API_KEY`` Docker secret) change.

When ``INDEXER_DELETION_ENABLED=true`` the indexer also runs a reconciler
that records tombstones for mbsync-flagged (``T``) Maildir files and reaps
them after a grace window. See ``src/reconciler.py``.
"""

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .attachment_indexing import (
    AttachmentWritePlan,
    apply_attachment_writes,
    prepare_attachment_writes,
)
from .chunker import chunk_message, mean_vector
from .database import EMBEDDING_DIM, Database
from .embedder import EmbeddingBackend, OpenAIEmbedder
from .parser import parse_email
from .queue import (
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_ON_MOVED,
    IndexingQueue,
)
from .queue import load_config_from_env as load_queue_config_from_env
from .quoting import strip_for_embedding
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from .threader import Threader
from .timings import StageTimings, TimingAggregator, format_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("indexer")

MAILDIR_PATH = Path(os.environ.get("MAILDIR_PATH", "/maildir"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))

# OpenAI-compatible embedder configuration.
#
# The default points at the host-side ``mlx-service`` on Apple Metal,
# reached from containers via OrbStack's ``host.docker.internal``. The
# service binds ``127.0.0.1:8001`` and exposes ``/v1/embeddings``.
#
# Any OpenAI-compatible provider works: replace ``EMBED_BASE_URL`` with
# the provider's base URL and set ``EMBED_API_KEY`` (Docker secret).
# The schema reserves a fixed 4096-dim vector — keep ``EMBED_MODEL``
# pointed at a 4096-dim model (Qwen3-Embedding-8B variants) or a schema
# migration is required.
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://host.docker.internal:8001/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "mlx-community/Qwen3-Embedding-8B-mxfp8")


def _read_embed_api_key() -> str:
    """Read the embedder API key from a Docker secret, then env, then empty.

    Mirrors the secret-then-env pattern used in mcp-server. An empty key
    is the local mlx-service case (no auth on loopback) and is not an
    error. The Docker secret path follows the existing
    ``/run/secrets/<name>`` convention; ``EMBED_API_KEY`` env is the
    fallback for non-Docker deployments.
    """
    secret_path = Path("/run/secrets/embed_api_key")
    if secret_path.exists():
        try:
            return secret_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("could not read /run/secrets/embed_api_key: %s", e)
    return os.environ.get("EMBED_API_KEY", "").strip()


EMBED_API_KEY = _read_embed_api_key()
# /tmp default is safe: container tmpfs, non-root user, overridable via env.
INDEXER_HEALTH_FILE = Path(os.environ.get("INDEXER_HEALTH_FILE", "/tmp/indexer-health"))  # nosec B108


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive int from the environment with a clamp + fallback.

    Used for the chunker token budgets so a typo or empty string falls back
    to the default rather than raising at startup. Mirrors the lenient parse
    used elsewhere (queue, reconciler) so operators get the same behavior
    across knobs.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid %s=%r; falling back to %d", name, raw, default)
        return default
    return max(minimum, value)


# How many texts the embedder client packs into a single
# ``/v1/embeddings`` HTTP call. Larger batches amortize per-request
# overhead — meaningful for cloud providers, marginal for the local
# mlx-service. The provider's own per-request input cap is the upper
# bound (DeepInfra accepts 100; OpenAI accepts 2048).
EMBED_BATCH_SIZE = _int_env("EMBED_BATCH_SIZE", 64)


# Chunker token budgets — see ``chunker.chunk_message`` for semantics.
# Defaults are sized for the MLX-served Qwen3-Embedding-8B context
# window. Qwen3-Embedding handles long context cleanly, so the
# default ``max`` is 1500 — fewer, larger chunks reduce embed call
# count and produce better mean-of-chunks thread vectors.
CHUNK_TARGET_TOKENS = _int_env("INDEXER_CHUNK_TARGET_TOKENS", 1000)
CHUNK_MAX_TOKENS = _int_env("INDEXER_CHUNK_MAX_TOKENS", 1500)
CHUNK_OVERLAP_TOKENS = _int_env("INDEXER_CHUNK_OVERLAP_TOKENS", 150, minimum=0)

# How many messages the initial-scan drainer accumulates before issuing
# a single batched embed call. Larger batches amortize the cloud-embedder
# round-trip across more messages — meaningful when EMBED_BASE_URL points
# at a remote provider (~150 ms RTT each), marginal against loopback
# mlx-service. Steady-state (watchdog) ingestion is unaffected: the
# watchdog path stays per-message because it sees 1-3 messages at a time.
INITIAL_INDEX_BATCH_SIZE = _int_env("INITIAL_INDEX_BATCH_SIZE", 50)

# Placeholder thread vector written by Phase 1 of the batched
# initial index. Phase 2c overwrites this with the mean of the
# thread's chunk embeddings once they're available. A zero vector is
# safe between phases: cosine/L2 similarity to a normalized query
# vector is constant, so a zero-vec thread cannot inflate ranking on
# any specific query — it just deprioritizes uniformly until the real
# vector lands. Keyword search via threads_fts is unaffected.
_ZERO_THREAD_VECTOR = [0.0] * EMBEDDING_DIM


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Attachment extraction — see ``src/extractors/`` for
# per-format implementations and ``.env.example`` for the operator-
# facing reference. Defaults are conservative: OCR enabled (most
# valuable for scanned receipts and screenshots), 10 MB attachment
# cap (skips huge backup zips), 20-page OCR cap (bounds CPU on
# scanned books).
INDEXER_ATTACHMENT_EXTRACTION_ENABLED = _bool_env("INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
INDEXER_OCR_ENABLED = _bool_env("INDEXER_OCR_ENABLED", True)
INDEXER_ATTACHMENT_MAX_BYTES = _int_env("INDEXER_ATTACHMENT_MAX_BYTES", 10_000_000, minimum=1)
INDEXER_OCR_MAX_PAGES = _int_env("INDEXER_OCR_MAX_PAGES", 20, minimum=1)
# Per-page OCR time ceiling. Tesseract is single-threaded and a
# crafted high-noise image (still inside ``INDEXER_ATTACHMENT_MAX_BYTES``)
# can keep it busy for minutes; combined with ``INDEXER_OCR_MAX_PAGES``
# that pins the worker for tens of minutes per PDF. Set to 0 to
# disable the timeout.
INDEXER_OCR_TIMEOUT_SECONDS = _int_env("INDEXER_OCR_TIMEOUT_SECONDS", 60, minimum=0)
# Page cap for the digital pypdf path. The OCR cap above doesn't bound
# this — a 5 MB text-only PDF can carry thousands of pages, and even
# at ~ms per page the indexer queue stalls. Set to 0 to disable.
INDEXER_PDF_MAX_DIGITAL_PAGES = _int_env("INDEXER_PDF_MAX_DIGITAL_PAGES", 500, minimum=0)
# 2,000,000 chars ~ 500 pages of dense OCR text. Bounds the
# ``attachment_extractions.extracted_text`` row size so a single huge
# scanned PDF can't blow up SQLite by storing tens of MB of text per
# attachment. Set to 0 to disable the cap.
INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS = _int_env(
    "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 2_000_000, minimum=0
)


def touch_health_file() -> None:
    INDEXER_HEALTH_FILE.touch(exist_ok=True)


class MaildirHandler(FileSystemEventHandler):
    """Watches Maildir for new email files and enqueues them for indexing.

    The callback path only enqueues — the actual parse / embed / upsert
    pipeline runs in the main loop via ``drain_queue``. Enqueue is a
    single SQLite write, so the watchdog's internal thread no longer
    blocks on a slow embed round-trip and a Watchdog event storm
    cannot overflow whatever buffer ``watchdog`` uses internally while
    an embed is in flight.
    """

    def __init__(
        self,
        db: Database,
        queue: IndexingQueue,
        reconciler: Reconciler | None = None,
    ):
        self.db = db
        self.queue = queue
        self.reconciler = reconciler

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Only enqueue files in cur/ or new/ subdirectories
        if path.parent.name in ("cur", "new"):
            self.queue.enqueue(str(path), REASON_ON_CREATED)

    def on_moved(self, event):
        # Two distinct move scenarios land here:
        # 1. Flag changes: mbsync renames files in-place within the same
        #    directory when flags change (e.g. ``msg:2,S`` → ``msg:2,SR``
        #    when the message is replied to). The source path is already
        #    in ``indexed_files``. Re-parsing and re-embedding would waste
        #    an embed round-trip and leave stale rows behind — the
        #    reconciler (or fall-through ``update_filepath``) just has to
        #    move the stored filepath to the new name.
        # 2. Maildir delivery: a message is first written under ``tmp/``
        #    and renamed into ``new/`` or ``cur/``. The source path is not
        #    in ``indexed_files`` (it was a temp file), so this is a new
        #    message that must be indexed (``on_created`` does not fire
        #    for rename destinations).
        if event.is_directory:
            return

        src_path = str(event.src_path)
        dest_path_obj = Path(event.dest_path)
        dest_path = str(dest_path_obj)

        if self.db.is_indexed(src_path):
            # Case 1: rename of an existing indexed message.
            if self.reconciler is not None:
                try:
                    self.reconciler.handle_moved(src_path, dest_path)
                except Exception as e:
                    log.error(f"reconciler on_moved failed: {e}")
            else:
                # Default deployment has no reconciler; still move the
                # indexed_files / message_thread_map filepath forward so
                # future lookups find the current on-disk name.
                try:
                    self.db.update_filepath(src_path, dest_path)
                except Exception as e:
                    log.error(f"update_filepath failed on rename: {e}")
            return

        # Case 2: new delivery — enqueue for the worker.
        if dest_path_obj.parent.name in ("cur", "new") and not self.db.is_indexed(dest_path):
            self.queue.enqueue(dest_path, REASON_ON_MOVED)


def _build_chunk_writes(
    thread,
    db: Database,
    embedder: EmbeddingBackend,
) -> list[tuple[object, list, dict[str, list[float]]]]:
    """Chunk + embed every newly-arrived message in ``thread``.

    ``thread.messages`` is the new arrivals only — existing messages
    already have chunks on disk and re-chunking them would burn embed
    cycles for no gain (chunk ids are deterministic from
    ``message_pk + index + text``, so the diff would be empty anyway).
    For new threads, ``thread.messages`` is the full thread (one
    message); for updates, it is the single newly-arrived reply.
    """
    chunk_writes: list[tuple[object, list, dict[str, list[float]]]] = []
    for msg in thread.messages:
        chunks = chunk_message(
            message_pk=msg.message_id,
            body_text=strip_for_embedding(msg.body_text or ""),
            target_tokens=CHUNK_TARGET_TOKENS,
            max_tokens=CHUNK_MAX_TOKENS,
            overlap_tokens=CHUNK_OVERLAP_TOKENS,
        )
        stored_ids = db.get_chunk_ids_for_message(msg.message_id)
        new_chunks = [c for c in chunks if c.chunk_id not in stored_ids]
        if new_chunks:
            # One batched HTTP call per message instead of one call per
            # chunk. Critical against cloud embedders where per-call
            # latency dominates; meaningful but smaller win against
            # mlx-service on loopback.
            vectors = embedder.embed_batch([c.text for c in new_chunks])
            embeddings_by_chunk_id = {c.chunk_id: v for c, v in zip(new_chunks, vectors)}
        else:
            embeddings_by_chunk_id = {}
        chunk_writes.append((msg, chunks, embeddings_by_chunk_id))
    return chunk_writes


def _build_attachment_plans(
    thread,
    db: Database,
    embedder: EmbeddingBackend,
) -> dict[str, list[AttachmentWritePlan]]:
    """Pre-compute per-message attachment plans outside any transaction.

    ``prepare_attachment_writes`` runs the extractor (OCR / pypdf /
    openpyxl) and the per-chunk Ollama embed calls — both too slow to
    hold inside a ``BEGIN IMMEDIATE``, since every outbound roundtrip
    would block the watchdog observer and the reconciler on the same
    DB lock. The caller applies the resulting plans inside the
    transaction.
    """
    plans_by_msg: dict[str, list[AttachmentWritePlan]] = {}
    if not INDEXER_ATTACHMENT_EXTRACTION_ENABLED:
        return plans_by_msg
    cap = (
        INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS
        if INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS > 0
        else None
    )
    for msg in thread.messages:
        if not msg.attachments:
            continue
        plans: list[AttachmentWritePlan] = []
        for occurrence_index, attachment in enumerate(msg.attachments):
            plans.append(
                prepare_attachment_writes(
                    attachment=attachment,
                    message_id=msg.message_id,
                    db=db,
                    embedder=embedder,
                    chunk_target_tokens=CHUNK_TARGET_TOKENS,
                    chunk_max_tokens=CHUNK_MAX_TOKENS,
                    chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
                    ocr_enabled=INDEXER_OCR_ENABLED,
                    max_bytes=INDEXER_ATTACHMENT_MAX_BYTES,
                    max_ocr_pages=INDEXER_OCR_MAX_PAGES,
                    ocr_timeout_seconds=INDEXER_OCR_TIMEOUT_SECONDS or None,
                    max_pdf_pages=INDEXER_PDF_MAX_DIGITAL_PAGES or None,
                    occurrence_index=occurrence_index,
                    max_extracted_chars=cap,
                )
            )
        plans_by_msg[msg.message_id] = plans
    return plans_by_msg


def _seed_thread_embedding(
    thread,
    db: Database,
    embedder: EmbeddingBackend,
    chunk_writes,
    attachment_plans_by_msg: dict[str, list[AttachmentWritePlan]],
) -> list[float]:
    """Pick the seed thread vector to write inside the transaction.

    The final vector is replaced after the chunk writes inside the same
    transaction (see ``_commit_indexing_writes``), so this seed only has
    to satisfy the ``threads_vec`` insert. Use the existing chunk mean
    when available, a zero vector if new chunks are about to land in
    this transaction (the post-write replace will overwrite it), and
    fall through to embedding the subject line only when there is no
    body or attachment text at all.
    """
    chunk_embeddings = db.get_thread_chunk_embeddings(thread.thread_id)
    if chunk_embeddings:
        return mean_vector(chunk_embeddings)
    has_incoming_chunks = any(chunks for _, chunks, _ in chunk_writes)
    has_incoming_attachment_chunks = any(
        plan.chunks for plans in attachment_plans_by_msg.values() for plan in plans
    )
    if has_incoming_chunks or has_incoming_attachment_chunks:
        return [0.0] * EMBEDDING_DIM
    fallback = thread.subject.strip() if thread.subject else "(empty thread)"
    return embedder.embed(fallback)


def _commit_indexing_writes(
    thread,
    db: Database,
    chunk_writes,
    attachment_plans_by_msg: dict[str, list[AttachmentWritePlan]],
    seed_embedding: list[float],
) -> None:
    """Persist a fully-prepared indexing payload inside one transaction.

    Body chunks, attachment chunks, and the recomputed thread vector
    all land or all roll back together. No network or extractor work
    happens here — every slow operation has already run during
    ``_build_chunk_writes`` / ``_build_attachment_plans``.
    """
    with db.transaction():
        db.upsert_thread(thread, seed_embedding)
        for msg, chunks, embeddings_by_chunk_id in chunk_writes:
            db.replace_message_chunks(
                message_id=msg.message_id,
                thread_id=thread.thread_id,
                chunks=chunks,
                embeddings_by_chunk_id=embeddings_by_chunk_id,
            )
            # Plans were prepared outside this transaction, so the
            # apply loop only does DB writes — no Ollama, no extractor.
            # Benign extractor outcomes (unsupported, empty, too_large,
            # failed parse) land as status rows here. Hard DB failures
            # propagate so the outer transaction rolls back and the
            # queue retries the whole message rather than committing a
            # half-indexed attachment.
            for plan in attachment_plans_by_msg.get(msg.message_id, ()):
                apply_attachment_writes(
                    plan=plan,
                    message_id=msg.message_id,
                    thread_id=thread.thread_id,
                    db=db,
                )

        updated_chunk_embeddings = db.get_thread_chunk_embeddings(thread.thread_id)
        if updated_chunk_embeddings:
            db.replace_thread_vector(thread.thread_id, mean_vector(updated_chunk_embeddings))


def _index_one_file(
    path: Path,
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
) -> tuple[bool, str, str | None, StageTimings]:
    """Run the parse → thread → embed → upsert pipeline for ``path``.

    Returns ``(succeeded, stage, error_message, timings)`` so the caller
    can record the specific failure stage in ``indexing_jobs.last_stage``
    and feed the per-stage durations into a rolling aggregator. A
    ``None`` ``Message`` from the parser is treated as a terminal
    success (no Message-ID, nothing retries will fix) — the queue row
    is deleted rather than retried indefinitely. Timings reflect only
    stages that ran; stages skipped due to an earlier failure stay 0.
    """
    parse_ms = thread_ms = embed_ms = db_write_ms = 0.0

    t0 = time.perf_counter()
    try:
        message = parse_email(path, maildir_root=MAILDIR_PATH)
    except FileNotFoundError as e:
        # The file was renamed or deleted between enqueue and parse.
        # Almost always this is mbsync appending an IMAP flag suffix
        # (``,U=42:2`` -> ``,U=42:2,S``) the moment it sets a flag,
        # which makes the original path permanently invalid. There is
        # nothing to retry — the indexer's Maildir watcher will see
        # the new path via ``IN_MOVED_TO`` and enqueue it under the
        # correct name. Distinct stage so the worker can drop the
        # row without consuming retry budget.
        parse_ms = (time.perf_counter() - t0) * 1000
        return False, "parse_skipped_missing", repr(e), StageTimings(parse_ms=parse_ms)
    except Exception as e:
        # Transient I/O on a path that DOES exist (PermissionError from
        # the mbsync chmod race) propagates so the queue routes it to
        # retry/backoff — the chmod resolves on a later sync cycle.
        # Other parse-content failures are caught inside parse_email
        # and surface as ``message is None`` below.
        parse_ms = (time.perf_counter() - t0) * 1000
        return False, "parse", repr(e), StageTimings(parse_ms=parse_ms)
    parse_ms = (time.perf_counter() - t0) * 1000
    if message is None:
        return True, "parse", None, StageTimings(parse_ms=parse_ms)

    t0 = time.perf_counter()
    try:
        thread = threader.assign_thread(message)
    except Exception as e:
        thread_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "thread",
            repr(e),
            StageTimings(parse_ms=parse_ms, thread_ms=thread_ms),
        )
    thread_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        chunk_writes = _build_chunk_writes(thread, db, embedder)
        attachment_plans_by_msg = _build_attachment_plans(thread, db, embedder)
        embedding = _seed_thread_embedding(
            thread, db, embedder, chunk_writes, attachment_plans_by_msg
        )
    except Exception as e:
        embed_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "embed",
            repr(e),
            StageTimings(parse_ms=parse_ms, thread_ms=thread_ms, embed_ms=embed_ms),
        )
    embed_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    try:
        _commit_indexing_writes(thread, db, chunk_writes, attachment_plans_by_msg, embedding)
    except Exception as e:
        db_write_ms = (time.perf_counter() - t0) * 1000
        return (
            False,
            "db_write",
            repr(e),
            StageTimings(
                parse_ms=parse_ms,
                thread_ms=thread_ms,
                embed_ms=embed_ms,
                db_write_ms=db_write_ms,
            ),
        )
    db_write_ms = (time.perf_counter() - t0) * 1000

    log.info(f"Indexed: {message.subject[:60]}")
    return (
        True,
        "db_write",
        None,
        StageTimings(
            parse_ms=parse_ms,
            thread_ms=thread_ms,
            embed_ms=embed_ms,
            db_write_ms=db_write_ms,
        ),
    )


def drain_queue(
    queue: IndexingQueue,
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    *,
    max_batch: int | None = None,
    timing_aggregator: TimingAggregator | None = None,
) -> int:
    """Process queued jobs until nothing is due, or ``max_batch`` have run.

    Returns the number of jobs attempted. Each iteration claims the
    next due row, runs the pipeline, and records success (delete row)
    or failure (increment attempts, schedule backoff, or dead-letter).
    Running with ``max_batch`` lets the main loop interleave reconciler
    passes and health-file refreshes with queue work so neither starves
    the other. ``timing_aggregator`` is fed every per-file timing so
    operators can see p50/p95 of each stage in the periodic summary.
    """
    attempted = 0
    while max_batch is None or attempted < max_batch:
        row = queue.claim_next()
        if row is None:
            break
        filepath = row["filepath"]
        succeeded, stage, error, timings = _index_one_file(Path(filepath), db, embedder, threader)
        if timing_aggregator is not None:
            timing_aggregator.record(timings)
        if succeeded:
            queue.mark_succeeded(filepath)
        elif stage == "parse_skipped_missing":
            # The file moved before parse could read it (mbsync flag
            # rename). Drop the row instead of consuming retry budget;
            # the renamed file's watchdog event re-enqueues it under
            # the correct name.
            queue.mark_skipped(filepath, reason="file_missing")
        else:
            queue.mark_failed(filepath, stage=stage, error=error or "")
        attempted += 1
    return attempted


HEALTH_REFRESH_EVERY = 25
# Emit a p50/p95/max timing summary at most this often. The aggregator
# itself has an independent rolling window — this constant only controls
# how often the line is logged, not how many samples back the percentiles
# look. Keeping it equal to ``HEALTH_REFRESH_EVERY`` lines summaries up
# with the same cadence as the health-file refresh.
TIMING_LOG_EVERY = 25


def _iter_maildir_messages(root: Path):
    """Yield every message file under ``root`` whose parent is ``cur`` or
    ``new``, at any nesting depth. mbsync ``SubFolders Verbatim`` can
    produce ``Clients/ABC/cur/msg`` — a flat ``iterdir`` over ``root``
    would miss every nested folder's mail."""
    for filepath in root.rglob("*"):
        if filepath.is_file() and filepath.parent.name in ("cur", "new"):
            yield filepath


@dataclass
class _BatchedMsg:
    """Per-message state carried through the two-phase batched indexer.

    Phase 1 populates ``msg`` and ``thread`` and commits the thread
    membership with a seed thread vector — the mean of the thread's
    existing chunk vectors when the thread is already indexed,
    otherwise a placeholder zero. Phase 2a populates the chunk +
    attachment-plan fields and the offsets that point each new chunk
    into the batch's flat embed-input list. Phase 2c reads the
    bulk-embedded vectors back through those offsets and applies the
    per-message DB writes, replacing the seed with the real
    mean-of-chunks (or subject-fallback) vector.
    """

    row: sqlite3.Row
    msg: Any  # parser.Message — Any avoids a circular type import
    thread: Any  # threader.Thread
    body_chunks: list[Any] = field(default_factory=list)
    new_body_chunks: list[Any] = field(default_factory=list)
    new_body_offsets: list[int] = field(default_factory=list)
    attach_plans: list[AttachmentWritePlan] = field(default_factory=list)
    attach_new_chunks: list[list[Any]] = field(default_factory=list)
    attach_offsets: list[list[int]] = field(default_factory=list)
    # Offset of this message's subject-fallback text in the batch's flat
    # ``all_texts`` list, or ``None`` when no fallback is needed. The
    # fallback is added in Phase 2a only when the message contributes
    # zero new chunks AND its parent thread has no existing chunks —
    # i.e. exactly the case where the Phase 1 seed is the placeholder
    # zero. Without this fallback the thread vector would be
    # permanently stuck at zero (search quality regression). Mirrors
    # the old ``_seed_thread_embedding`` subject fallback.
    subject_fallback_offset: int | None = None
    parse_ms: float = 0.0
    thread_ms: float = 0.0
    phase1_ms: float = 0.0
    chunk_ms: float = 0.0


def _phase1_commit_thread(
    row: sqlite3.Row,
    db: Database,
    threader: Threader,
    queue: IndexingQueue,
) -> _BatchedMsg | None:
    """Phase 1 of the batched indexer for one message.

    Parse → thread → ``upsert_thread`` with a seed vector
    (``mean`` of the thread's existing chunk vectors when the thread
    is already indexed; placeholder zero for new / chunk-less
    threads). Returns a populated ``_BatchedMsg`` on success. On any
    failure, marks the queue row appropriately and returns ``None`` so
    the caller skips the message without aborting the whole batch.
    """
    filepath = row["filepath"]

    t0 = time.perf_counter()
    try:
        msg = parse_email(Path(filepath), maildir_root=MAILDIR_PATH)
    except FileNotFoundError:
        # mbsync flag-rename race: file moved between enqueue and parse.
        # Watchdog's IN_MOVED_TO will re-enqueue under the new name.
        queue.mark_skipped(filepath, reason="file_missing")
        return None
    except Exception as e:
        queue.mark_failed(filepath, stage="parse", error=repr(e))
        return None
    parse_ms = (time.perf_counter() - t0) * 1000
    if msg is None:
        # Parser returned None for a terminal reason (no Message-ID, etc.)
        # — drop the row so the queue does not retry indefinitely.
        queue.mark_succeeded(filepath)
        return None

    t0 = time.perf_counter()
    try:
        thread = threader.assign_thread(msg)
    except Exception as e:
        queue.mark_failed(filepath, stage="thread", error=repr(e))
        return None
    thread_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    # Seed the Phase 1 thread vector. Two cases:
    #   1. Thread already exists with chunk vectors — use their mean
    #      so a Phase 2 failure (embed outage, etc.) does not corrupt
    #      a previously-good vector. Without this, every new message
    #      on an existing thread temporarily zeroes the parent's
    #      vector during Phase 1, and a queue retry / dead-letter
    #      would leave it permanently zero.
    #   2. New thread (or existing thread with no chunk vectors yet)
    #      — use the placeholder zero. Phase 2c will replace it via
    #      either mean-of-new-chunks or the subject fallback.
    existing_chunk_embs = db.get_thread_chunk_embeddings(thread.thread_id)
    seed_vector = mean_vector(existing_chunk_embs) if existing_chunk_embs else _ZERO_THREAD_VECTOR
    try:
        # Phase 1 commit: write thread + message_thread_map + indexed_files
        # with the seed vector. Threading state is durable before
        # Phase 2 runs, so the next message in the batch sees this
        # message's thread when computing its own thread assignment.
        db.upsert_thread(thread, seed_vector)
    except Exception as e:
        queue.mark_failed(filepath, stage="thread_commit", error=repr(e))
        return None
    phase1_ms = (time.perf_counter() - t0) * 1000

    return _BatchedMsg(
        row=row,
        msg=msg,
        thread=thread,
        parse_ms=parse_ms,
        thread_ms=thread_ms,
        phase1_ms=phase1_ms,
    )


def _phase2a_collect_chunks(
    state: _BatchedMsg,
    db: Database,
    all_texts: list[str],
) -> tuple[bool, str | None]:
    """Phase 2a: chunk the body and attachments WITHOUT embedding.

    Appends every new chunk's text to the shared ``all_texts`` list and
    records the offsets on ``state`` so Phase 2c can read its vectors
    back. Returns ``(True, None)`` on success or ``(False, error)`` on
    a chunk/extract failure (rare — usually only attachment OCR errors)
    so the caller can mark the queue row failed without aborting the
    rest of the batch.
    """
    msg = state.msg
    t0 = time.perf_counter()
    try:
        body_chunks = chunk_message(
            message_pk=msg.message_id,
            body_text=strip_for_embedding(msg.body_text or ""),
            target_tokens=CHUNK_TARGET_TOKENS,
            max_tokens=CHUNK_MAX_TOKENS,
            overlap_tokens=CHUNK_OVERLAP_TOKENS,
        )
        stored_ids = db.get_chunk_ids_for_message(msg.message_id)
        new_body = [c for c in body_chunks if c.chunk_id not in stored_ids]
        new_body_offsets: list[int] = []
        for c in new_body:
            new_body_offsets.append(len(all_texts))
            all_texts.append(c.text)

        attach_plans: list[AttachmentWritePlan] = []
        attach_new_chunks: list[list[Any]] = []
        attach_offsets: list[list[int]] = []
        if INDEXER_ATTACHMENT_EXTRACTION_ENABLED and msg.attachments:
            cap = (
                INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS
                if INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS > 0
                else None
            )
            for occurrence_index, attachment in enumerate(msg.attachments):
                # ``embedder=None`` defers the embed step — the plan
                # comes back with empty embeddings_by_chunk_id and
                # Phase 2c populates it from the batched embed result.
                plan = prepare_attachment_writes(
                    attachment=attachment,
                    message_id=msg.message_id,
                    db=db,
                    embedder=None,
                    chunk_target_tokens=CHUNK_TARGET_TOKENS,
                    chunk_max_tokens=CHUNK_MAX_TOKENS,
                    chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
                    ocr_enabled=INDEXER_OCR_ENABLED,
                    max_bytes=INDEXER_ATTACHMENT_MAX_BYTES,
                    max_ocr_pages=INDEXER_OCR_MAX_PAGES,
                    ocr_timeout_seconds=INDEXER_OCR_TIMEOUT_SECONDS or None,
                    max_pdf_pages=INDEXER_PDF_MAX_DIGITAL_PAGES or None,
                    occurrence_index=occurrence_index,
                    max_extracted_chars=cap,
                )
                if plan.chunks:
                    stored_attach_ids = db.get_chunk_ids_for_message(
                        msg.message_id, attachment_id=attachment.content_hash
                    )
                    plan_new = [c for c in plan.chunks if c.chunk_id not in stored_attach_ids]
                else:
                    plan_new = []
                plan_offsets: list[int] = []
                for c in plan_new:
                    plan_offsets.append(len(all_texts))
                    all_texts.append(c.text)
                attach_plans.append(plan)
                attach_new_chunks.append(plan_new)
                attach_offsets.append(plan_offsets)
        # Subject-fallback path: when this message contributes zero new
        # chunks AND the parent thread has no existing chunks, embed the
        # subject (or a sentinel string) so the thread vector is not
        # permanently stuck at the Phase 1 placeholder zero-vector.
        # Mirrors the old ``_seed_thread_embedding`` per-message
        # behavior. The fallback text rides through Phase 2b inside the
        # same batched embed call as everyone else's chunks.
        has_new_chunks = bool(new_body) or any(plan_new for plan_new in attach_new_chunks)
        if not has_new_chunks and not db.get_thread_chunk_embeddings(state.thread.thread_id):
            fallback_text = state.thread.subject.strip() if state.thread.subject else ""
            if not fallback_text:
                fallback_text = "(empty thread)"
            state.subject_fallback_offset = len(all_texts)
            all_texts.append(fallback_text)
    except Exception as e:
        # Phase 2a is "extract + chunk" only — no DB writes. A failure
        # here marks this message failed but leaves Phase 1's thread
        # commit in place (keyword-searchable but vectorless until
        # retry succeeds or the operator dead-letters this row).
        return False, repr(e)

    state.body_chunks = body_chunks
    state.new_body_chunks = new_body
    state.new_body_offsets = new_body_offsets
    state.attach_plans = attach_plans
    state.attach_new_chunks = attach_new_chunks
    state.attach_offsets = attach_offsets
    state.chunk_ms = (time.perf_counter() - t0) * 1000
    return True, None


def _phase2c_commit_vectors(
    state: _BatchedMsg,
    db: Database,
    vectors: list[list[float]],
) -> tuple[bool, str | None]:
    """Phase 2c: per-message DB transaction for body + attachments + thread vec.

    Reads vectors out of the shared batch result via the offsets
    captured in Phase 2a, then writes everything inside one
    ``with db.transaction()`` block so chunks/vectors/thread-vector
    either all land or all roll back for this message.
    """
    msg = state.msg
    thread = state.thread

    body_embs = {
        c.chunk_id: vectors[i] for c, i in zip(state.new_body_chunks, state.new_body_offsets)
    }
    for plan, plan_new, plan_offsets in zip(
        state.attach_plans, state.attach_new_chunks, state.attach_offsets
    ):
        plan.embeddings_by_chunk_id = {
            c.chunk_id: vectors[i] for c, i in zip(plan_new, plan_offsets)
        }

    try:
        with db.transaction():
            db.replace_message_chunks(
                message_id=msg.message_id,
                thread_id=thread.thread_id,
                chunks=state.body_chunks,
                embeddings_by_chunk_id=body_embs,
            )
            for plan in state.attach_plans:
                apply_attachment_writes(
                    plan=plan,
                    message_id=msg.message_id,
                    thread_id=thread.thread_id,
                    db=db,
                )
            # Replace the Phase 1 seed thread vector. Three cases
            # mirror the old ``_seed_thread_embedding`` logic:
            #   1. Thread now has chunks (this message contributed
            #      some, or earlier messages already had them) → use
            #      the mean of those chunk vectors.
            #   2. No chunks anywhere on the thread, but Phase 2a
            #      reserved a subject-fallback slot in the embed batch
            #      (blank-body, no-attachment-chunks message — would
            #      otherwise be permanently stuck at zero) → use the
            #      subject-embedded vector.
            #   3. Neither — leave the placeholder zero in place.
            #      Should not occur in practice; if it does, the next
            #      message on the thread will overwrite via case 1.
            chunk_embs = db.get_thread_chunk_embeddings(thread.thread_id)
            if chunk_embs:
                db.replace_thread_vector(thread.thread_id, mean_vector(chunk_embs))
            elif state.subject_fallback_offset is not None:
                db.replace_thread_vector(thread.thread_id, vectors[state.subject_fallback_offset])
    except Exception as e:
        return False, repr(e)
    return True, None


def _drain_queue_batched(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
    *,
    batch_size: int,
    timing_aggregator: TimingAggregator,
) -> int:
    """Drain the queue in two-phase batches.

    Phase 1 commits thread membership per-message with a seed thread
    vector — ``mean(existing chunk vectors)`` for already-indexed
    threads, placeholder zero for new ones — so (a) the next message
    in the batch's threader can see this message's thread, and (b) a
    Phase 2 failure cannot regress an already-good thread vector to
    zero.
    Phase 2a chunks the body + attachment text without embedding.
    Phase 2b issues a single ``embed_batch`` covering every new chunk
    across the whole batch — this is the optimization: ~25k single-
    HTTP-call messages becomes ~500 multi-message HTTP calls against a
    cloud embedder.
    Phase 2c commits the chunk + vector writes per message and
    overwrites the Phase 1 seed thread vector with the real
    mean-of-chunks vector.

    Failure isolation:

    * Phase 1 error for one message — that message marked failed,
      others continue.
    * Phase 2a (chunk/extract) error for one message — marked failed,
      Phase 1's commit stays. Vector-less but text-searchable.
    * Phase 2b (embed) error — entire in-flight batch's queue rows
      marked failed (queue retry on next pass). Phase 1 commits
      remain; the next pass re-runs Phase 1 (idempotent upsert) plus
      Phase 2.
    * Phase 2c (DB write) error for one message — marked failed,
      others succeed.
    """
    processed = 0
    while True:
        # ---- Gather batch + Phase 1 ----
        # Snapshot up to batch_size distinct queued rows in one query
        # so the gather loop cannot re-claim the same row repeatedly
        # while we defer mark_succeeded to Phase 2c.
        rows = queue.claim_batch(batch_size)
        if not rows:
            break
        batch: list[_BatchedMsg] = []
        for row in rows:
            entry = _phase1_commit_thread(row, db, threader, queue)
            processed += 1
            if entry is not None:
                batch.append(entry)
            touch_health_file()

        if not batch:
            continue

        # ---- Phase 2a: collect chunks across batch (no embed) ----
        all_texts: list[str] = []
        survivors: list[_BatchedMsg] = []
        for entry in batch:
            ok, err = _phase2a_collect_chunks(entry, db, all_texts)
            if ok:
                survivors.append(entry)
            else:
                queue.mark_failed(entry.row["filepath"], stage="chunk", error=err or "")

        if not survivors:
            continue

        # Refresh the heartbeat just before the bulk embed so a slow
        # cloud-embedder round-trip (potentially tens of seconds for a
        # full batch of chunks) doesn't age the health file past
        # HEALTH_MAX_AGE_SECONDS while no per-message touch fires.
        touch_health_file()

        # ---- Phase 2b: bulk embed across batch ----
        t_embed_start = time.perf_counter()
        try:
            vectors = embedder.embed_batch(all_texts) if all_texts else []
        except Exception as e:
            log.error(
                "batched embed failed for %d texts (batch=%d msgs): %r",
                len(all_texts),
                len(survivors),
                e,
            )
            for entry in survivors:
                queue.mark_failed(entry.row["filepath"], stage="embed", error=repr(e))
            continue
        embed_ms = (time.perf_counter() - t_embed_start) * 1000
        # Attribute embed time evenly across the batch for telemetry.
        per_msg_embed_ms = embed_ms / max(1, len(survivors))

        # ---- Phase 2c: per-message vector commits ----
        for entry in survivors:
            t0 = time.perf_counter()
            ok, err = _phase2c_commit_vectors(entry, db, vectors)
            db_write_ms = (time.perf_counter() - t0) * 1000
            if ok:
                queue.mark_succeeded(entry.row["filepath"])
                timing_aggregator.record(
                    StageTimings(
                        parse_ms=entry.parse_ms,
                        thread_ms=entry.thread_ms,
                        embed_ms=per_msg_embed_ms,
                        db_write_ms=db_write_ms,
                    )
                )
            else:
                queue.mark_failed(entry.row["filepath"], stage="db_write", error=err or "")
            touch_health_file()

        if processed and processed % TIMING_LOG_EVERY < batch_size:
            line = format_summary(timing_aggregator.summary())
            if line:
                log.info(line)

    return processed


def initial_index(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
):
    """Enqueue every unindexed Maildir message and drain the queue.

    Refreshes the health file after every processed message so that
    long initial indexes (large mailboxes, slow Ollama embeddings, OCR
    on scanned PDFs) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the
    healthcheck and cause the container to be reported unhealthy
    mid-scan. (A single message that itself takes longer than
    ``HEALTH_MAX_AGE_SECONDS`` will still trip the healthcheck — that
    case would need a heartbeat hook inside ``_index_one_file``.)

    Routing the initial scan through the queue — rather than indexing
    files inline — means a crash or Ollama outage mid-scan leaves the
    untouched work durably queued instead of dropped. The next restart
    resumes from ``indexing_jobs`` rather than rescanning the whole
    Maildir and relying on ``is_indexed`` to filter.
    """
    log.info("Running initial index scan...")
    enqueued = 0
    skipped_dead = 0
    for filepath in _iter_maildir_messages(MAILDIR_PATH):
        path_str = str(filepath)
        if db.is_indexed(path_str):
            continue
        # Don't resurrect dead-lettered files on routine startup. The
        # initial scan only proves "this file exists on disk" — not
        # that anything about its content has changed since the last
        # attempt failed. Watchdog IN_MOVED_TO / IN_CREATED still go
        # through ``enqueue`` (which DOES reset prior state via
        # INSERT OR REPLACE) because those events DO indicate the
        # file changed. Without this skip, every container restart
        # re-runs the same 5-attempt × 30s backoff cascade against
        # the same poison-pill payloads — observed to add up to
        # ~30 minutes of wasted Ollama load per dead file per restart.
        if queue.is_dead(path_str):
            skipped_dead += 1
            continue
        queue.enqueue(path_str, REASON_INITIAL_SCAN)
        enqueued += 1
    log.info(
        "Initial index: enqueued %d message(s), skipped %d dead-lettered.",
        enqueued,
        skipped_dead,
    )

    timing_aggregator = TimingAggregator(window=200)
    log.info(
        "Initial index: draining queue with batch_size=%d (cross-message embed batching).",
        INITIAL_INDEX_BATCH_SIZE,
    )
    processed = _drain_queue_batched(
        db,
        embedder,
        threader,
        queue,
        batch_size=INITIAL_INDEX_BATCH_SIZE,
        timing_aggregator=timing_aggregator,
    )
    # Always emit a final summary at the end of the initial scan, even
    # if the count was not a multiple of ``TIMING_LOG_EVERY`` — the
    # operator wants to see the cost of the scan they just ran.
    final_line = format_summary(timing_aggregator.summary())
    if final_line:
        log.info(final_line)
    log.info(f"Initial index complete: {processed} job(s) processed.")


def _validate_embedding_dim(embedder: EmbeddingBackend) -> None:
    """Probe the running embedder once at startup and verify its output
    dimension matches the schema-reserved ``EMBEDDING_DIM``.

    Mismatched dimensions would otherwise fail on the first
    ``upsert_thread`` with a cryptic sqlite-vec error. Fail fast at
    startup with a clear, actionable message instead.
    """
    probe = embedder.embed("dimension probe")
    if len(probe) != EMBEDDING_DIM:
        raise SystemExit(
            f"Embedder produced {len(probe)}-dim vectors, but the SQLite "
            f"schema reserves {EMBEDDING_DIM}-dim (threads_vec "
            f"FLOAT[{EMBEDDING_DIM}]). Either switch to a model that "
            f"outputs {EMBEDDING_DIM}-dim vectors, or migrate the schema."
        )


def _log_reconciler_config(cfg: ReconcilerConfig) -> None:
    if not cfg.enabled:
        log.info("Deletion reconciliation: disabled (set INDEXER_DELETION_ENABLED=true to enable)")
        return
    log.info(
        "Deletion reconciliation: enabled "
        "(grace=%dd, sweep=%ds, max_batch=%.1f%%, force=%s, unlink=%s)",
        cfg.grace_days,
        cfg.sweep_interval_secs,
        cfg.max_batch_pct * 100,
        cfg.force,
        cfg.unlink_on_reap,
    )


def main():
    log.info("Starting indexer...")
    log.info(f"  Maildir: {MAILDIR_PATH}")
    log.info(f"  SQLite:  {SQLITE_PATH}")
    log.info(f"  Embedder: {EMBED_BASE_URL} (model={EMBED_MODEL}, batch={EMBED_BATCH_SIZE})")
    if EMBED_API_KEY:
        log.info("  Embedder API key: present (Bearer auth enabled)")

    db = Database(SQLITE_PATH)
    embedder = OpenAIEmbedder(
        base_url=EMBED_BASE_URL,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        batch_size=EMBED_BATCH_SIZE,
    )
    threader = Threader(db)
    touch_health_file()

    queue_cfg = load_queue_config_from_env(os.environ)
    queue = IndexingQueue(
        db,
        max_attempts=queue_cfg["max_attempts"],
        base_backoff_seconds=queue_cfg["base_backoff_seconds"],
    )
    log.info(
        "Indexing queue: max_attempts=%d base_backoff=%ds",
        queue_cfg["max_attempts"],
        queue_cfg["base_backoff_seconds"],
    )
    queue_depth = queue.stats()
    if queue_depth["queued"] or queue_depth["dead"]:
        log.info(
            "Queue carry-over from previous run: queued=%d dead=%d",
            queue_depth["queued"],
            queue_depth["dead"],
        )

    reconciler_config = load_config_from_env(os.environ)
    _log_reconciler_config(reconciler_config)
    reconciler: Reconciler | None = None
    if reconciler_config.enabled:
        reconciler = Reconciler(
            db, embedder, threader, reconciler_config, maildir_root=MAILDIR_PATH
        )

    # Wait for the mlx-service /health endpoint, then warm the model.
    embedder.wait_for_ready()

    # Verify the running model matches the schema's reserved vector dim.
    _validate_embedding_dim(embedder)

    # Index existing emails
    initial_index(db, embedder, threader, queue)
    touch_health_file()

    # Always-on startup rename sweep. mbsync renames files in place for
    # any flag change (e.g. seen ``S`` → seen+replied ``SR``) and when
    # promoting from ``new/`` to ``cur/``. Events that land while the
    # indexer is offline would otherwise leave stale filepaths in
    # ``indexed_files``, which makes later lookups on the renamed file
    # miss. sweep_paths() only updates filepath rows; it does not
    # tombstone missing files, so running it unconditionally preserves
    # the opt-in posture of deletion reconciliation.
    try:
        sweep_paths(db)
    except Exception as e:
        log.error(f"startup rename sweep failed: {e}")

    # Startup reconciliation sweep — detect tombstones and path renames that
    # landed while the indexer was offline. Safe to run every startup: it only
    # writes to pending_deletions and updates stored filepaths.
    if reconciler is not None:
        try:
            reconciler.sweep()
            reconciler.reap()
        except Exception as e:
            log.error(f"startup reconciliation failed: {e}")

    # Watch for new emails
    handler = MaildirHandler(db, queue, reconciler=reconciler)
    observer = Observer()
    observer.schedule(handler, str(MAILDIR_PATH), recursive=True)
    observer.start()
    log.info("Watching Maildir for new emails...")

    last_reconcile = time.monotonic()
    timing_aggregator = TimingAggregator(window=200)
    drained_since_log = 0
    try:
        while True:
            touch_health_file()
            # Drain any queued indexing jobs before yielding to the
            # reconciler so newly-arrived mail is visible in search
            # quickly. ``max_batch`` caps each pass so a large initial
            # enqueue (or a burst from an mbsync sync) does not starve
            # the reconciler or the health-file refresh.
            try:
                drained = drain_queue(
                    queue,
                    db,
                    embedder,
                    threader,
                    max_batch=HEALTH_REFRESH_EVERY,
                    timing_aggregator=timing_aggregator,
                )
                drained_since_log += drained
                if drained_since_log >= TIMING_LOG_EVERY:
                    line = format_summary(timing_aggregator.summary())
                    if line:
                        log.info(line)
                    drained_since_log = 0
            except Exception as e:
                log.error(f"queue drain failed: {e}")
            if reconciler is not None:
                now = time.monotonic()
                if now - last_reconcile >= reconciler_config.sweep_interval_secs:
                    try:
                        reconciler.sweep()
                        reconciler.reap()
                    except Exception as e:
                        log.error(f"periodic reconciliation failed: {e}")
                    last_reconcile = now
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
