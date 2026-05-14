"""
Indexer entry point.
Watches the Maildir for new/changed emails, parses and threads them,
generates embeddings via an OpenAI-compatible /v1/embeddings endpoint,
and writes to the SQLite index.

The embedder is operator-supplied: any OpenAI-compatible provider
works (OpenAI proper, remote alternatives like DeepInfra / OpenRouter,
or a host-side server the operator installs themselves: LM Studio,
vLLM, ``mlx_lm.server``, TEI). Configure via ``EMBED_MODEL`` +
``EMBED_API_KEY`` Docker secret (both required, non-empty);
``EMBED_BASE_URL`` is optional — leave it empty to use the openai
SDK's documented default (OpenAI proper), or set it to point at any
other endpoint. The required ``EMBED_API_KEY`` is the explicit-intent
signal that makes an empty ``EMBED_BASE_URL`` unambiguous: an
operator with a real ``sk-...`` has unambiguously chosen their
provider. Operators pointing at an unauthenticated host-side server
set ``EMBED_BASE_URL`` to the host endpoint and supply any
placeholder string for the key. ``EMBED_MODE`` is the wire-shape
selector kept for symmetry with the other layers; only ``openai`` is
valid today.

When ``INDEXER_DELETION_ENABLED=true`` the indexer also runs a reconciler
that records tombstones for mbsync-flagged (``T``) Maildir files and reaps
them after a grace window. See ``src/reconciler.py``.
"""

import logging
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .attachment_indexing import (
    AttachmentWritePlan,
    apply_attachment_writes,
    prepare_attachment_writes,
)
from .chunker import MessageChunk, chunk_message, mean_vector
from .database import EMBEDDING_DIM, Database
from .embedder import EmbeddingBackend, OpenAIEmbedder, scrub_embed_error
from .parser import Message, OversizedMessageError, parse_email
from .queue import (
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_ON_MOVED,
    REASON_RECOVERY,
    IndexingQueue,
)
from .queue import load_config_from_env as load_queue_config_from_env
from .quoting import strip_for_embedding
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from .threader import Thread, Threader
from .timings import StageTimings, TimingAggregator, format_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("indexer")

MAILDIR_PATH = Path(os.environ.get("MAILDIR_PATH", "/maildir"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))

# OpenAI-compatible embedder configuration. The operator supplies the
# provider. Set ``EMBED_MODEL`` to a model id served at the chosen
# endpoint; the schema reserves a fixed 4096-dim vector so pick a
# 4096-dim model (Qwen3-Embedding-8B variants) or run a schema
# migration. Set ``EMBED_BASE_URL`` to point at any compliant /v1 base
# URL, or leave it empty to use the openai SDK's documented default
# (OpenAI proper at ``https://api.openai.com/v1``). The bearer
# credential is loaded from the ``embed_api_key`` Docker secret or
# ``EMBED_API_KEY`` env and is required (non-empty); the key is the
# explicit-intent signal that makes empty-URL unambiguous, and
# unauthenticated host-side servers accept any placeholder string.
# ``EMBED_MODE`` is kept as a config knob for symmetry with
# ``INFERENCE_MODE`` / ``RERANK_MODE`` but only accepts ``openai``
# today — embed is the headline retrieval feature and the indexer
# cannot function without it, so there is no disabled mode.
_EMBED_MODES = frozenset({"openai"})


def _normalize_embed_mode(raw: str) -> str:
    mode = raw.strip().lower()
    if mode in _EMBED_MODES:
        return mode
    raise ValueError("EMBED_MODE must be 'openai'")


EMBED_MODE = _normalize_embed_mode(os.environ.get("EMBED_MODE", "openai"))
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "")


def _validate_embed_config() -> None:
    """Raise at startup when the embedder is misconfigured.

    Validation runs in ``main()`` rather than at module load so test
    files can ``from src import main`` to import helper functions
    without supplying a full embedder config. The container entrypoint
    always reaches ``main()`` first, so the operator-facing failure
    surface is identical.

    ``EMBED_API_KEY`` is required (non-empty); ``EMBED_MODEL`` is too.
    ``EMBED_BASE_URL`` may be empty: that is interpreted as "use the
    SDK default" (OpenAI proper via the openai SDK), and the required
    ``EMBED_API_KEY`` is the explicit-intent signal that makes the
    interpretation unambiguous — an operator with a real ``sk-...``
    has unambiguously chosen their provider. Symmetric with how
    ``INFERENCE_MODE=anthropic`` and ``INFERENCE_MODE=openai`` treat
    empty ``INFERENCE_BASE_URL``. Operators pointing at an
    unauthenticated host-side server (LM Studio, vLLM,
    ``mlx_lm.server``, TEI) supply any placeholder string for
    ``EMBED_API_KEY``; the compat server ignores the bearer header.
    """
    if not EMBED_MODEL:
        raise ValueError("EMBED_MODEL must be set when EMBED_MODE='openai'")
    if not EMBED_API_KEY:
        raise ValueError("EMBED_API_KEY must be set when EMBED_MODE='openai'")
    # Reject URLs that embed a ``user:pass@host`` userinfo authority.
    # The resolved base URL flows into the startup log line naming the
    # wire endpoint, so embedded credentials would leak to container
    # logs / journald. The credential model puts every secret in a
    # Docker-secrets file (``.secrets/embed_api_key.txt``). Mirrors the
    # same guard in ``scripts/validate-env.sh`` so a deployment that
    # skipped that script still fails closed instead of leaking.
    if EMBED_BASE_URL and "@" in urllib.parse.urlsplit(EMBED_BASE_URL).netloc:
        raise ValueError(
            "EMBED_BASE_URL must not embed credentials (user:pass@host). Put "
            "the API key in .secrets/embed_api_key.txt instead."
        )


def _read_embed_api_key() -> str:
    """Read the embedder API key from a Docker secret, then env, then empty.

    Mirrors the secret-then-env pattern used in mcp-server. The Docker
    secret path follows the existing ``/run/secrets/<name>`` convention;
    ``EMBED_API_KEY`` env is the fallback for non-Docker deployments.

    An empty return value is not an error here — the startup contract
    is enforced by ``_validate_embed_config()``, which fails closed if
    no key is set. Splitting "read" from "require" keeps the reader
    purely about source-of-truth precedence (secret > env), and the
    validator owns the non-empty rule. For unauthenticated host-side
    servers the operator supplies a placeholder string (e.g.
    ``unauthenticated``); the SDK sends it as a bearer token that
    compat servers ignore.

    Fail-closed posture: when the Docker secret file *exists* but cannot
    be read (perms regression, mount issue), this is a deployment
    misconfiguration — propagate the error so the indexer fails to
    start rather than silently sending an empty bearer token (or worse,
    a stale env value the operator thought the secret had superseded).
    The env fallback only kicks in when the secret file is absent.
    """
    secret_path = Path("/run/secrets/embed_api_key")
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
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
# overhead — meaningful for remote providers, marginal for a host-side
# server on loopback. The provider's own per-request input cap is the
# upper bound (DeepInfra accepts 100; OpenAI accepts 2048).
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
# a single batched embed call. Larger batches amortize the embed
# round-trip across more messages — meaningful when EMBED_BASE_URL
# points at a remote provider (~150 ms RTT each), marginal against a
# host-side server on loopback.
INITIAL_INDEX_BATCH_SIZE = _int_env("INITIAL_INDEX_BATCH_SIZE", 50)

# Steady-state (post-initial-scan) batch size for the main-loop drain.
# Smaller than the initial-scan size because steady-state typically sees
# 1-3 messages per pass, but routing through the same batched path means
# (a) a burst from an mbsync sync still gets one bulk embed call instead
# of one HTTP round-trip per message (decisive against any cloud
# embedder), and (b) the initial-scan and steady-state code paths share
# one Phase 1/2 implementation rather than diverging on seed-vector
# selection.
STEADY_STATE_BATCH_SIZE = _int_env("INDEXER_STEADY_STATE_BATCH_SIZE", 8)

# How often (seconds) the main loop calls ``Database.wal_checkpoint_truncate``.
# The single shared sqlite3 connection used by ``Database`` keeps a WAL
# read snapshot open for the duration of the indexer process; without an
# explicit truncate-checkpoint the WAL file grows monotonically. 10 min
# keeps the file size bounded without churning IO.
WAL_CHECKPOINT_INTERVAL_SECS = _int_env("INDEXER_WAL_CHECKPOINT_INTERVAL_SECS", 600, minimum=60)

# How often (seconds) the main loop runs ``_recover_zero_vector_threads``.
# The periodic sweep recovers retryable shapes — chunkless zero-vector
# threads whose queue row is still 'queued' or has been cleaned up —
# but does NOT auto-resurrect dead-lettered rows. Dead = operator-
# visible terminal state; clearing it requires explicit intervention
# (or a future operator-rescue tool that calls
# ``_recover_zero_vector_threads(..., resurrect_dead=True)``).
# See ``_recover_zero_vector_threads`` for the full policy.
RECOVERY_SWEEP_INTERVAL_SECS = _int_env("INDEXER_RECOVERY_SWEEP_INTERVAL_SECS", 1800, minimum=60)

# Phase 1 seed for genuinely new threads (the only branch that uses
# this constant). Phase 1's seed-selection runs a three-case priority
# chain:
#   1. Thread has chunk vectors → mean(chunks).
#   2. No chunks but a prior threads_vec row carries a non-zero
#      embedding → preserve that. Covers chunkless subject-fallback
#      threads — protects them from Phase 2 failure clobbering.
#   3. Neither → this zero placeholder. Truly new threads, or
#      post-crash recovery on a thread that already had a zero row.
# A zero vector is safe between phases: cosine/L2 similarity to a
# normalized query vector is constant, so a zero-vec thread cannot
# inflate ranking on any specific query — it just deprioritizes
# uniformly until Phase 2c lands the real vector. Keyword search via
# threads_fts is unaffected.
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
                    log.error("reconciler on_moved failed: %s", e)
            else:
                # Default deployment has no reconciler; still move the
                # indexed_files / message_thread_map filepath forward so
                # future lookups find the current on-disk name.
                try:
                    self.db.update_filepath(src_path, dest_path)
                except Exception as e:
                    log.error("update_filepath failed on rename: %s", e)
            return

        # Case 2: new delivery — enqueue for the worker.
        if dest_path_obj.parent.name in ("cur", "new") and not self.db.is_indexed(dest_path):
            self.queue.enqueue(dest_path, REASON_ON_MOVED)


# ``_index_one_file`` and ``drain_queue`` below are compatibility shims
# that delegate to the canonical batched pipeline. The previous
# per-message orchestration (``_build_chunk_writes``,
# ``_build_attachment_plans``, ``_seed_thread_embedding``, and
# ``_commit_indexing_writes``) was deleted to eliminate the
# seed-vector duplication across the two paths. Phase 1's three-case
# priority chain (chunks-mean / preserved non-zero prior / zero
# placeholder) and Phase 2c's subject-fallback path now drive every
# write, regardless of whether one file or fifty arrive together.


def _index_one_file(
    path: Path,
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
) -> tuple[bool, str, str | None, StageTimings]:
    """Run one file through the unified batched pipeline.

    Compatibility shim that pre-dates the path consolidation. Returns
    ``(succeeded, stage, error_message, timings)`` by inspecting the
    queue state after a single-row drain. The four-tuple shape is
    preserved for tests that pre-date the consolidation; the
    ``timings`` member is a zero ``StageTimings`` because per-stage
    timing now flows through the ``TimingAggregator`` plumbed into
    ``_drain_queue_batched``, not through this entrypoint.

    A private 1-attempt queue is used so any failure transitions
    immediately to ``status='dead'``, mirroring the prior
    "fail-on-first-error" semantics callers expected from
    ``_index_one_file`` (the original returned ``(False, stage, ...)``
    on the first exception).
    """
    private_queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
    private_queue.enqueue(str(path), reason="index_one_file")
    aggregator = TimingAggregator(window=4)
    _drain_queue_batched(
        db,
        embedder,
        threader,
        private_queue,
        batch_size=1,
        max_passes=1,
        timing_aggregator=aggregator,
    )
    row = db._conn.execute(
        "SELECT status, last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
        (str(path),),
    ).fetchone()
    if row is None:
        # Row deleted: succeeded, terminal-success (no Message-ID), or
        # mark_skipped (file vanished between enqueue and parse —
        # mbsync flag-rename race). Distinguish via filesystem check:
        # file missing → skip; file present → succeeded.
        if not path.exists():
            return (
                False,
                "parse_skipped_missing",
                "FileNotFoundError(file moved between enqueue and parse)",
                StageTimings(),
            )
        return True, "db_write", None, StageTimings()
    # Row exists at status='dead' (max_attempts=1 above transitions on
    # the first failure) or rarely 'queued' (mid-cascade if a future
    # change relaxes max_attempts). Either way the file is not indexed.
    return (
        False,
        row["last_stage"] or "unknown",
        row["last_error"] or "",
        StageTimings(),
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
    """Compatibility shim that delegates to ``_drain_queue_batched``.

    Production code paths (``initial_index`` and the steady-state
    main loop) call ``_drain_queue_batched`` directly. This wrapper
    preserves the older signature for tests that pre-date the
    consolidation. ``max_batch=None`` drains to empty in batches of
    8; ``max_batch=N`` runs at most one pass with ``batch_size=N``.
    """
    if timing_aggregator is None:
        timing_aggregator = TimingAggregator(window=4)
    if max_batch is None:
        return _drain_queue_batched(
            db,
            embedder,
            threader,
            queue,
            batch_size=8,
            timing_aggregator=timing_aggregator,
        )
    return _drain_queue_batched(
        db,
        embedder,
        threader,
        queue,
        batch_size=max_batch,
        max_passes=1,
        timing_aggregator=timing_aggregator,
    )


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
    membership with a seed thread vector chosen from a three-case
    priority chain: ``mean(existing chunk vectors)`` when the thread
    is already indexed with content; the prior ``threads_vec`` row
    when the thread is chunkless but has a non-zero embedding (covers
    subject-fallback threads); placeholder zero only for genuinely
    new threads. Phase 2a populates the chunk + attachment-plan
    fields and the offsets that point each new chunk into the batch's
    flat embed-input list. Phase 2c reads the bulk-embedded vectors
    back through those offsets and applies the per-message DB writes,
    replacing the seed with the real mean-of-chunks (or
    subject-fallback) vector.
    """

    row: sqlite3.Row
    msg: Message
    thread: Thread
    body_chunks: list[MessageChunk] = field(default_factory=list)
    new_body_chunks: list[MessageChunk] = field(default_factory=list)
    new_body_offsets: list[int] = field(default_factory=list)
    attach_plans: list[AttachmentWritePlan] = field(default_factory=list)
    attach_new_chunks: list[list[MessageChunk]] = field(default_factory=list)
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
    except OversizedMessageError as e:
        # File exceeds INDEXER_PARSE_MAX_BYTES. Terminal under current
        # config — retrying will find the same oversized file and burn
        # embed budget. Dead-letter so the durable row survives restarts
        # (the file is still on disk, so deleting the row would let
        # ``initial_index`` re-enqueue it on every container start). The
        # ``is_dead`` gate then skips the file thereafter, and operators
        # see the entry in ``queue.stats()['dead']``.
        queue.mark_dead_terminal(filepath, stage="parse", error=f"oversized: {e}")
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
    # Seed the Phase 1 thread vector. Three cases, in priority order:
    #   1. Thread has chunk vectors — use their mean. This is the
    #      canonical seed for already-indexed threads with content.
    #   2. No chunk vectors but a prior threads_vec row exists with
    #      a non-zero embedding — preserve that. Covers chunkless
    #      threads whose vector came from a subject fallback (an
    #      earlier blank-body message in the same thread). Without
    #      this branch, a new sibling message's Phase 1 commit would
    #      clobber the subject vector with zero, and a Phase 2
    #      failure or dead-letter would leave it permanently zero.
    #   3. Neither — truly new thread, or existing thread with a
    #      zero-vector row from a prior crashed batch. Use the
    #      placeholder zero; Phase 2c will replace it with either
    #      mean-of-new-chunks or the subject fallback.
    # ``get_phase1_seed_state`` short-circuits with empty/None for
    # brand-new threads via a cheap PK existence check, so the bulk
    # case during an initial scan does one PK lookup instead of two
    # empty reads.
    existing_chunk_embs, prior_vec = db.get_phase1_seed_state(thread.thread_id)
    if existing_chunk_embs:
        seed_vector = mean_vector(existing_chunk_embs)
    elif prior_vec is not None and any(v != 0.0 for v in prior_vec):
        seed_vector = prior_vec
    else:
        seed_vector = _ZERO_THREAD_VECTOR
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

    Subject fallback gating depends ONLY on COMMITTED state — the
    earlier-shape "skip fallback if an earlier batch sibling queued
    chunks for the same thread" optimization conflated pending and
    committed work: if that earlier sibling's Phase 2c then failed,
    the chunkless successor would commit cleanly with no chunks, no
    fallback, and a thread vector stuck at the Phase 1 zero
    placeholder (operator-invisible retrieval-quality regression).
    Reserving an extra embed-batch slot per chunkless sibling on a
    chunk-bearing thread costs one BPE encode and one embed payload
    entry — negligible compared to the bulk-embed savings, and the
    correctness story is straightforward: fallback is reserved iff
    the message has no new chunks AND the thread has no committed
    chunks at the moment of the check.
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
        attach_new_chunks: list[list[MessageChunk]] = []
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
        # chunks AND the parent thread has no committed chunks, embed
        # the subject (or a sentinel string) so the thread vector is
        # not permanently stuck at the Phase 1 placeholder zero.
        # Mirrors the old ``_seed_thread_embedding`` per-message
        # behavior. The fallback text rides through Phase 2b inside the
        # same batched embed call as everyone else's chunks.
        #
        # The check is on COMMITTED chunks only (``thread_has_chunks``),
        # not on pending in-batch chunks. An earlier batch sibling that
        # queued chunks for the same thread but whose Phase 2c then
        # fails would otherwise leave this chunkless successor
        # committing cleanly with a zero thread vector — see the
        # docstring rationale above. Reserving an extra fallback slot
        # when an earlier sibling also produced chunks is harmless:
        # Phase 2c's three-case priority chain prefers
        # ``mean(committed chunks)`` over the fallback embedding, so
        # the fallback only takes effect when nothing else committed.
        #
        # ``thread_has_chunks`` is a single-row existence check —
        # avoids the per-message blob-unpack churn that
        # ``get_thread_chunk_embeddings`` would impose on chatty
        # threads where this gate fires for every chunkless arrival.
        has_new_chunks = bool(new_body) or any(plan_new for plan_new in attach_new_chunks)
        if not has_new_chunks and not db.thread_has_chunks(state.thread.thread_id):
            # Source the fallback text from the thread's stored
            # ``display_subject`` rather than from ``state.msg.subject``.
            # ``display_subject`` is the OLDEST message's original-case
            # subject, maintained by ``upsert_thread``'s merge across
            # every arrival. Reading from there keeps the fallback
            # vector STABLE across successive chunkless arrivals: using
            # ``state.msg.subject`` made each new chunkless message
            # overwrite the prior thread vector with its own subject
            # embedding (msg1's "Quarterly Review" → msg2's "Re: Quarterly
            # Review" → msg3's "Fwd: ..."), producing arrival-order-
            # dependent thread vectors on a chunkless thread.
            #
            # Phase 1 already committed this message's row via
            # ``upsert_thread``, so the display_subject reflecting the
            # oldest-seen message is in the DB before this read. Falls
            # through to the message's own subject for legacy v12 rows
            # where display_subject is NULL (an indexer pass will
            # refresh those over time).
            stored_display = db.get_thread_display_subject(state.thread.thread_id)
            fallback_text = (stored_display or state.msg.subject or "").strip()
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

    # ISO-8601 representation of the source message's Date: header.
    # Stamped onto every chunk row (body + attachment) so timeline
    # retrieval can order by message time instead of insert time —
    # see ``replace_message_chunks`` and the v18 migration.
    msg_date_iso = msg.date.isoformat()
    try:
        with db.transaction():
            db.replace_message_chunks(
                message_id=msg.message_id,
                thread_id=thread.thread_id,
                chunks=state.body_chunks,
                embeddings_by_chunk_id=body_embs,
                message_date=msg_date_iso,
            )
            for plan in state.attach_plans:
                apply_attachment_writes(
                    plan=plan,
                    message_id=msg.message_id,
                    thread_id=thread.thread_id,
                    db=db,
                    message_date=msg_date_iso,
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
    max_passes: int | None = None,
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

    ``max_passes`` bounds how many ``claim_batch`` rounds run before
    returning. ``None`` (the default) drains until the queue is empty
    — the right shape for ``initial_index``. Steady-state callers in
    the main loop pass ``max_passes=1`` so each tick interleaves
    cleanly with the reconciler sweep, WAL checkpoint, and health-file
    refresh instead of starving them on a long burst.

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
    passes = 0
    while True:
        if max_passes is not None and passes >= max_passes:
            break
        passes += 1
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
        # Each entry can take seconds to many tens of seconds for a
        # large attachment-heavy message (PDF chunking, OCR, etc.). The
        # cumulative wall-clock for a batch_size=50 batch can blow past
        # HEALTH_MAX_AGE_SECONDS, so touch the heartbeat after every
        # entry — not just before/after the bulk embed.
        all_texts: list[str] = []
        survivors: list[_BatchedMsg] = []
        for entry in batch:
            ok, err = _phase2a_collect_chunks(entry, db, all_texts)
            if ok:
                survivors.append(entry)
            else:
                queue.mark_failed(entry.row["filepath"], stage="chunk", error=err or "")
            touch_health_file()

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
            vectors = (
                embedder.embed_batch(all_texts, on_batch_complete=touch_health_file)
                if all_texts
                else []
            )
        except Exception as e:
            # Scrub the error before persistence: ``APIStatusError`` can
            # echo input fragments (email body text) on 4xx, and
            # ``last_error`` rides into ``indexing_jobs.last_error`` +
            # operator log sinks. ``scrub_embed_error`` keeps full repr
            # for safe error shapes (connection / timeout / our own
            # integrity-check ``RuntimeError``) and trims SDK status
            # errors to type + status_code.
            err_repr = scrub_embed_error(e)
            log.error(
                "batched embed failed for %d texts (batch=%d msgs): %s",
                len(all_texts),
                len(survivors),
                err_repr,
            )
            for entry in survivors:
                queue.mark_failed(entry.row["filepath"], stage="embed", error=err_repr)
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
                # ``db_write_ms`` aggregates BOTH DB-write phases:
                # Phase 1's ``upsert_thread`` (recorded as
                # ``entry.phase1_ms``) plus the Phase 2c per-message
                # transaction (``db_write_ms`` measured above). Without
                # the Phase 1 contribution the ``db_write`` and
                # ``total`` columns in the periodic summary
                # under-report wall-clock — Phase 1 commits are
                # idempotent upserts but still cost SQLite IO time.
                timing_aggregator.record(
                    StageTimings(
                        parse_ms=entry.parse_ms,
                        thread_ms=entry.thread_ms,
                        chunk_ms=entry.chunk_ms,
                        embed_ms=per_msg_embed_ms,
                        db_write_ms=entry.phase1_ms + db_write_ms,
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


def _recover_zero_vector_threads(
    db: Database, queue: IndexingQueue, *, resurrect_dead: bool = False
) -> int:
    """Re-enqueue messages stuck on chunkless zero-vector threads.

    Policy: dead-lettered rows are an operator-visible terminal state
    and are NOT auto-resurrected. The recovery sweep only rescues
    files in queued / no-row state — those represent in-flight or
    cleaned-up work the indexer can safely retry. A row at
    ``status='dead'`` stayed in ``indexing_jobs`` precisely so the
    operator could see that ``max_attempts`` was exhausted; clearing
    that state without operator intent would burn embedder quota
    against the same payload on every container restart and undo
    ``initial_index``'s deliberate ``is_dead`` skip.

    What the sweep handles:

    * **No queue row** (Phase 1 committed but the row was somehow
      cleaned up out-of-band) — re-enqueue with a fresh budget.
    * **Crash mid-batch** (process killed between Phase 1 and
      ``mark_failed``/``mark_succeeded``) — the row stays at
      ``status='queued'`` with the same attempts count as when the
      worker claimed it, and the next ``_drain_queue_batched`` will
      pick it up. ``has_pending_row`` returns True so this branch
      is skipped here, and that's correct — the queue itself owns
      the recovery.
    * **Healthy chunkless subject-fallback threads** (non-zero
      ``threads_vec``) are filtered out at the DB query level.

    What the sweep does NOT handle by default:

    * **Dead-lettered rows** — left alone. To rescue a dead-lettered
      file, an operator confirms the underlying cause is fixed and
      either:

      - deletes the dead row directly (``DELETE FROM indexing_jobs
        WHERE filepath = '...';``), after which the next periodic
        recovery sweep — or the next ``initial_index`` at restart
        — sees the file as a no-row zero-vector candidate and
        re-enqueues it via the ``no queue row`` branch above; or
      - calls this function with ``resurrect_dead=True`` from a
        one-off rescue tool, which clears the dead status via
        ``enqueue``'s ``INSERT OR REPLACE`` in one step.

      Note: simply touching the Maildir file does NOT re-enqueue
      it. The watchdog handles ``on_created`` and ``on_moved``
      events but not ``on_modified``, and ``initial_index``
      consults ``queue.is_dead`` and skips dead-lettered paths
      regardless of file activity.

      The ``resurrect_dead=True`` flag stays on the API for that
      operator tool; no production call site uses it.

    Skips files that already have a 'queued' row (active retry
    cascade in flight; clobbering its row would reset the attempts
    counter mid-cascade).

    Returns the number of files re-enqueued for visibility in logs.
    """
    candidates = db.find_zero_vector_chunkless_thread_filepaths()
    if not candidates:
        return 0

    re_enqueued = 0
    skipped_pending = 0
    skipped_dead = 0
    for filepath in candidates:
        if queue.has_pending_row(filepath):
            skipped_pending += 1
            continue
        if not resurrect_dead and queue.is_dead(filepath):
            skipped_dead += 1
            continue
        queue.enqueue(filepath, REASON_RECOVERY)
        re_enqueued += 1

    if re_enqueued or skipped_pending or skipped_dead:
        # Log shape: split the "what happened" facts from the
        # "what's next" implication so the implication only applies
        # to the rows that will actually move. The previous wording
        # ("Their next drain pass should complete the indexing")
        # incorrectly suggested skipped-dead rows would also drain;
        # they will not until an operator intervenes.
        log.warning(
            "recovery sweep: re-enqueued %d zero-vector chunkless "
            "message(s); skipped %d already in active retry; skipped %d "
            "dead-lettered (resurrect_dead=%s).",
            re_enqueued,
            skipped_pending,
            skipped_dead,
            resurrect_dead,
        )
        if re_enqueued:
            log.info(
                "recovery sweep: %d re-enqueued file(s) will be processed on the next drain pass.",
                re_enqueued,
            )
        if skipped_dead and not resurrect_dead:
            log.warning(
                "recovery sweep: %d dead-lettered file(s) remain parked. "
                "They will NOT be drained automatically — see "
                "_recover_zero_vector_threads docstring for the operator "
                "rescue paths.",
                skipped_dead,
            )
    return re_enqueued


def initial_index(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
):
    """Enqueue every unindexed Maildir message and drain the queue.

    Refreshes the health file after every processed message so that
    long initial indexes (large mailboxes, slow embedding service, OCR
    on scanned PDFs) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the
    healthcheck and cause the container to be reported unhealthy
    mid-scan. (A single message that itself takes longer than
    ``HEALTH_MAX_AGE_SECONDS`` will still trip the healthcheck — that
    case would need a heartbeat hook inside ``_index_one_file``.)

    Routing the initial scan through the queue — rather than indexing
    files inline — means a crash or embedding service outage mid-scan leaves the
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
        # ~30 minutes of wasted embedding service load per dead file per restart.
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

    # Recovery sweep — re-enqueue messages stuck on chunkless zero-vector
    # threads from a prior crash mid-batch (queued row that mark_failed /
    # mark_succeeded never reached). The standard walk above misses these
    # because ``is_indexed=True``. Dead-lettered rows are intentionally
    # NOT touched here — that policy is uniform across startup and
    # periodic, matching ``initial_index``'s ``is_dead`` skip and the
    # durable queue's bounded-retry contract. See
    # ``_recover_zero_vector_threads`` for the rationale and the
    # ``resurrect_dead=True`` opt-in path. Run BEFORE the drain so
    # recovery rows ride the same batched-index pass as fresh enqueues.
    _recover_zero_vector_threads(db, queue)

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
    log.info("Initial index complete: %d job(s) processed.", processed)


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
    _validate_embed_config()
    log.info("Starting indexer...")
    log.info("  Maildir: %s", MAILDIR_PATH)
    log.info("  SQLite:  %s", SQLITE_PATH)

    db = Database(SQLITE_PATH)
    embedder = OpenAIEmbedder(
        base_url=EMBED_BASE_URL,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        batch_size=EMBED_BATCH_SIZE,
    )
    # Log the resolved wire endpoint after construction. ``EMBED_BASE_URL=""``
    # intentionally means "use the SDK default" (OpenAI proper); printing
    # the raw env value would hide that the indexer is actually pointing
    # at api.openai.com when the operator forgot to wire an
    # unauthenticated host-side server. ``OpenAIEmbedder.base_url`` reads
    # the URL back from the SDK after fallback resolution, matching the
    # mcp-server inference / rerank log lines.
    log.info(
        "  Embedder: %s (model=%s, batch=%d)",
        embedder.base_url,
        EMBED_MODEL,
        EMBED_BATCH_SIZE,
    )
    if EMBED_API_KEY:
        log.info("  Embedder API key: present (Bearer auth enabled)")
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

    # Wait for the embedder to answer, then warm the model.
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
        log.error("startup rename sweep failed: %s", e)

    # Startup reconciliation sweep — detect tombstones and path renames that
    # landed while the indexer was offline. Safe to run every startup: it only
    # writes to pending_deletions and updates stored filepaths.
    if reconciler is not None:
        try:
            reconciler.sweep()
            reconciler.reap()
        except Exception as e:
            log.error("startup reconciliation failed: %s", e)

    # Watch for new emails
    handler = MaildirHandler(db, queue, reconciler=reconciler)
    observer = Observer()
    observer.schedule(handler, str(MAILDIR_PATH), recursive=True)
    observer.start()
    log.info("Watching Maildir for new emails...")

    last_reconcile = time.monotonic()
    last_recovery_sweep = time.monotonic()
    last_wal_checkpoint = time.monotonic()
    timing_aggregator = TimingAggregator(window=200)
    drained_since_log = 0
    try:
        while True:
            touch_health_file()
            # Drain any queued indexing jobs before yielding to the
            # reconciler so newly-arrived mail is visible in search
            # quickly. Steady state goes through the same batched path
            # initial_index uses, with ``max_passes=1`` so each tick
            # processes at most ``STEADY_STATE_BATCH_SIZE`` messages
            # before yielding to the reconciler / WAL checkpoint /
            # recovery sweep. A burst from an mbsync sync still lands
            # in one bulk embed call instead of N per-message HTTP
            # round-trips.
            try:
                drained = _drain_queue_batched(
                    db,
                    embedder,
                    threader,
                    queue,
                    batch_size=STEADY_STATE_BATCH_SIZE,
                    timing_aggregator=timing_aggregator,
                    max_passes=1,
                )
                drained_since_log += drained
                if drained_since_log >= TIMING_LOG_EVERY:
                    line = format_summary(timing_aggregator.summary())
                    if line:
                        # Tag the periodic timing summary with current
                        # queue depth so operators see when work is
                        # backing up without grepping a separate log.
                        depth = queue.stats()
                        log.info(
                            "%s queued=%d dead=%d",
                            line,
                            depth["queued"],
                            depth["dead"],
                        )
                    drained_since_log = 0
            except Exception as e:
                log.error("queue drain failed: %s", e)

            now = time.monotonic()
            if reconciler is not None:
                if now - last_reconcile >= reconciler_config.sweep_interval_secs:
                    try:
                        reconciler.sweep()
                        reconciler.reap()
                    except Exception as e:
                        log.error("periodic reconciliation failed: %s", e)
                    last_reconcile = now

            # Recovery sweep: re-enqueue messages on chunkless
            # zero-vector threads that are STILL retryable. A
            # transient embedder bug that left a row queued-but-stuck
            # heals on its own once the underlying cause clears.
            # Dead-lettered rows are left alone (default policy,
            # uniform with the startup path) — see
            # ``_recover_zero_vector_threads`` for why.
            if now - last_recovery_sweep >= RECOVERY_SWEEP_INTERVAL_SECS:
                try:
                    _recover_zero_vector_threads(db, queue)
                except Exception as e:
                    log.error("periodic recovery sweep failed: %s", e)
                last_recovery_sweep = now

            # WAL checkpoint: keep the WAL file size bounded over a
            # long-running container. The indexer holds a single
            # writer connection for the life of the process; that
            # connection's read snapshot prevents SQLite's automatic
            # checkpoint thresholds from truncating the WAL, so an
            # explicit periodic ``wal_checkpoint(TRUNCATE)`` is what
            # reclaims space on the writer side.
            if now - last_wal_checkpoint >= WAL_CHECKPOINT_INTERVAL_SECS:
                try:
                    busy, _log_pages, ckpt_pages = db.wal_checkpoint_truncate()
                    if busy:
                        log.debug(
                            "wal_checkpoint busy=%d (a reader pinned WAL frames; "
                            "next pass will retry)",
                            busy,
                        )
                    elif ckpt_pages:
                        log.debug("wal_checkpoint truncated %d page(s)", ckpt_pages)
                except Exception as e:
                    log.error("wal checkpoint failed: %s", e)
                last_wal_checkpoint = now

            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
