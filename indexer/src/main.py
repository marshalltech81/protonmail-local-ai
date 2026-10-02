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

By default (mirror mode) the indexer also runs a reconciler that records
tombstones for mbsync-flagged (``T``) Maildir files and reaps them after a
grace window; ``INDEXER_DELETION_ENABLED=false`` (archive mode) turns it
off. See ``src/reconciler.py``.
"""

import logging
import os
import sqlite3
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .attachment_indexing import (
    AttachmentWritePlan,
    apply_attachment_writes,
    prepare_attachment_writes,
    reruns_once_ocr_is_on,
)
from .chunker import (
    MessageChunk,
    chunk_message,
    estimate_tokens,
    mean_vector,
    truncate_to_tokens,
)
from .database import EMBEDDING_DIM, Database
from .embedder import (
    EMBED_FAILURE_CONFIGURATION,
    EMBED_FAILURE_REJECTED_INPUT,
    EMBED_FAILURE_UNCERTAIN,
    EmbeddingBackend,
    EmbedResponseError,
    OpenAIEmbedder,
    classify_embed_failure,
    scrub_embed_error,
)
from .entities import AuthorityRules, AuthorityRulesError, load_authority_rules
from .extractors import ExtractionResult, is_stale_extractor
from .folder_watch import FolderWatchRefresher
from .maildir import (
    SYNC_STAMP_NAME,
    SyncStamp,
    is_trashed,
    parse_sync_stamp_rename,
    read_sync_stamp,
)
from .parser import Message, OversizedMessageError, _derive_folder, parse_email
from .queue import (
    ERROR_CLASS_OPERATOR,
    ERROR_CLASS_RETRYABLE,
    INTERRUPTED_STAGE,
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_ON_MOVED,
    REASON_RECOVERY,
    REASON_REEXTRACT,
    REASON_RESCAN,
    IndexingQueue,
)
from .queue import load_config_from_env as load_queue_config_from_env
from .quoting import strip_for_embedding
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from .stall_guard import StallGuard
from .threader import Thread, Threader, reply_subject_line
from .timings import StageTimings, TimingAggregator, format_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("indexer")

MAILDIR_PATH = Path(os.environ.get("MAILDIR_PATH", "/maildir"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))
# Operator source-authority rules (``config/authority.toml.example``),
# at the fixed path where compose mounts ``./config`` read-only.
# Absent: every entity is unclassified. Malformed: startup fails.
AUTHORITY_RULES_PATH = Path("/config/authority.toml")

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
    """Read an int >= ``minimum`` from the environment.

    Unset or empty yields ``default``. A non-integer or a value below
    ``minimum`` raises ``ValueError`` naming the variable, so a typo
    stops startup instead of silently changing behaviour (#481). Same
    rule as ``queue.load_config_from_env`` and
    ``reconciler.load_config_from_env``.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if value < minimum:
        raise ValueError(f"{name}={raw!r} must be >= {minimum}")
    return value


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


def _check_chunk_budgets(target: int, max_tokens: int, overlap: int) -> None:
    """Raise ``ValueError`` unless the chunk budgets fit together.

    ``chunker.chunk_message`` requires ``target <= max`` and
    ``overlap < target`` (each budget's own minimum is enforced by
    ``_int_env``). It checks this per message, so a bad combination
    would start cleanly and then dead-letter every message; check it
    once here instead (#507).
    """
    if target > max_tokens:
        raise ValueError(
            f"INDEXER_CHUNK_TARGET_TOKENS={target} must be <= INDEXER_CHUNK_MAX_TOKENS={max_tokens}"
        )
    if overlap >= target:
        raise ValueError(
            f"INDEXER_CHUNK_OVERLAP_TOKENS={overlap} must be < INDEXER_CHUNK_TARGET_TOKENS={target}"
        )


_check_chunk_budgets(CHUNK_TARGET_TOKENS, CHUNK_MAX_TOKENS, CHUNK_OVERLAP_TOKENS)

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
# An open connection does not pin the WAL; only an open read transaction
# does. SQLite's automatic checkpoint lets the WAL be reused from the
# start once its frames are checkpointed, but it never shrinks the file,
# so the WAL stays at its high-water size after a burst of writes. The
# explicit truncate-checkpoint reclaims that space. 10 min keeps the file
# size bounded without churning IO.
WAL_CHECKPOINT_INTERVAL_SECS = _int_env("INDEXER_WAL_CHECKPOINT_INTERVAL_SECS", 600, minimum=60)

# How often (seconds) the main loop runs ``_recover_zero_vector_threads``.
# The periodic sweep recovers retryable shapes — chunkless zero-vector
# threads whose queue row is still 'queued' or has been cleaned up —
# but does NOT auto-resurrect dead-lettered rows. Dead = operator-
# visible terminal state; clearing it requires explicit intervention
# (``make requeue-dead``).
# See ``_recover_zero_vector_threads`` for the full policy. The same
# cadence drives the periodic Maildir rescan
# (``_enqueue_unindexed_messages``).
RECOVERY_SWEEP_INTERVAL_SECS = _int_env("INDEXER_RECOVERY_SWEEP_INTERVAL_SECS", 1800, minimum=60)

# Phase 1 seed for new threads and already-zero chunkless ones (the
# only branch that uses this constant). Phase 1's seed-selection runs a three-case priority
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
    """Read a boolean from the environment.

    Unset or empty yields ``default``; any value outside the accepted
    vocabulary raises ``ValueError`` so a typo such as
    ``INDEXER_OCR_ENABLED=tru`` stops startup instead of disabling the
    feature (#481). Same vocabulary as ``reconciler.load_config_from_env``.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}={raw!r} is not recognized; use true or false")


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
# Longest one unit of work — a message's parse, or one attachment's
# extraction — may run before the stall guard exits the process for
# Compose to restart (``stall_guard``). Each attachment restarts the
# clock, so a message with many slow attachments is fine; one scanned
# PDF legitimately takes up to ``INDEXER_OCR_MAX_PAGES`` x
# ``INDEXER_OCR_TIMEOUT_SECONDS`` plus its render (~21 min at the
# defaults). Set to 0 to disable.
INDEXER_MESSAGE_TIMEOUT_SECONDS = _int_env("INDEXER_MESSAGE_TIMEOUT_SECONDS", 3600, minimum=0)
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
    # Liveness for get_mailbox_status rides on every health heartbeat
    # (per message, per embed batch), so a long drain pass never reads
    # as a stopped indexer.
    if _ingestion_state is not None:
        _ingestion_state.maybe_record(time.monotonic())


def _extraction_heartbeat() -> None:
    """``touch_health_file`` for an extractor's per-page progress.

    The dispatcher records any exception an extractor raises as a
    ``failed`` extraction of the payload, so a heartbeat write error must
    not escape into it. A file that cannot be refreshed goes stale, and
    the healthcheck reports that.
    """
    try:
        touch_health_file()
    except OSError as e:
        log.warning("health file refresh failed: %s", type(e).__name__)


class _IngestionStateRecorder:
    """Writes the last acknowledged mbsync sync and the indexer's
    liveness into ``ingestion_state`` for mcp-server's
    ``get_mailbox_status``.

    A sync is acknowledged only once every message it delivered is
    queued: when the watcher handles the stamp's rename (watchdog
    dispatches events in order, so the sync's delivery events were
    handled first), or when a Maildir walk that started after the sync
    finishes. A stamp merely present on disk proves nothing — the
    watcher may still be behind on that sync's deliveries.

    Writes at most once per ``interval_secs``; mcp-server's staleness
    thresholds are minutes.
    """

    def __init__(self, db: Database, maildir_root: Path, interval_secs: float = 30):
        self.db = db
        self.maildir_root = maildir_root
        self.interval_secs = interval_secs
        self._acked: SyncStamp | None = None
        self._last_write: float | None = None

    def read_stamp(self) -> SyncStamp | None:
        """The stamp on disk now, for acknowledging after a walk."""
        try:
            return read_sync_stamp(self.maildir_root)
        except (OSError, ValueError) as e:
            log.warning("unreadable mbsync sync stamp %s: %s", SYNC_STAMP_NAME, e)
            return None

    def acknowledge(self, stamp: SyncStamp | None) -> None:
        # Called from the watcher thread and the main thread; keep the
        # newest so a walk that read an older stamp cannot move it back.
        # Timestamps share one UTC ISO format, so they compare as strings.
        # An acknowledged stamp ahead of our clock (the clock rolled back)
        # yields to any new one; otherwise every later sync would sort
        # earlier and be dropped until the clock caught up (#332).
        acked = self._acked
        if stamp is None:
            return
        if (
            acked is None
            or stamp.completed_at > acked.completed_at
            or datetime.fromisoformat(acked.completed_at) > datetime.now(UTC)
        ):
            self._acked = stamp

    def maybe_record(self, now: float) -> None:
        if self._last_write is not None and now - self._last_write < self.interval_secs:
            return
        acked = self._acked
        try:
            self.db.record_ingestion_state(
                sync_completed_at=acked.completed_at if acked else None,
                sync_interval_secs=acked.sync_interval_secs if acked else None,
                seen_at=datetime.now(UTC).isoformat(),
            )
        except sqlite3.Error as e:
            log.error("recording ingestion state failed: %s", e)
            return
        self._last_write = now


# Set by ``main``; ``touch_health_file`` reports liveness through it.
_ingestion_state: _IngestionStateRecorder | None = None


class MaildirHandler(FileSystemEventHandler):
    """Watches Maildir for new email files and enqueues them for indexing.

    The callback path only enqueues — the actual parse / embed / upsert
    pipeline runs in the main loop via ``_drain_queue_batched``. Enqueue is a
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
        ingestion_state: _IngestionStateRecorder | None = None,
        sync_completed: threading.Event | None = None,
        directory_created: threading.Event | None = None,
    ):
        self.db = db
        self.queue = queue
        self.reconciler = reconciler
        self.ingestion_state = ingestion_state
        # Set on each mbsync sync stamp; the main loop then re-watches
        # folders the sync's permission repair made readable (#516).
        self.sync_completed = sync_completed
        # Set on every directory the watch reports created: watchdog
        # cannot watch one mbsync created 0700, so the next refresh
        # re-schedules the watch.
        self.directory_created = directory_created

    def _is_reaped_or_deleted(self, path: str | Path) -> bool:
        # With deletion reconciliation enabled, a T-flagged file is
        # deleted upstream. After a reap (``unlink_on_reap=False``) its
        # .eml stays on disk unindexed; enqueueing it would resurrect
        # the message into search. Same rule as the Maildir walk's
        # ``skip_trashed``.
        return self.reconciler is not None and is_trashed(path)

    def on_created(self, event):
        if event.is_directory:
            if self.directory_created is not None:
                self.directory_created.set()
            return
        path = Path(event.src_path)
        # Only enqueue files in cur/ or new/ subdirectories
        if path.parent.name in ("cur", "new") and not self._is_reaped_or_deleted(path):
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

        # mbsync renamed a new last-sync stamp into place. Acknowledge the
        # sync named by the temporary file, not the stamp's current
        # content: a later sync may already have replaced it while its
        # deliveries are still behind this event.
        if dest_path_obj.name == SYNC_STAMP_NAME and dest_path_obj.parent == MAILDIR_PATH:
            stamp = parse_sync_stamp_rename(src_path)
            if stamp is None:
                log.warning("ignoring unrecognized rename onto the mbsync sync stamp")
            else:
                if self.ingestion_state is not None:
                    self.ingestion_state.acknowledge(stamp)
                if self.sync_completed is not None:
                    self.sync_completed.set()
            return

        if self.db.is_indexed(src_path):
            # Case 1: rename of an existing indexed message. Flag renames
            # stay in one folder; a move across folders also carries the
            # new folder, recorded atomically with the locator. If that
            # write fails it rolls back whole: the destination stays
            # unindexed and the periodic Maildir walk re-indexes it.
            dest_folder = _derive_folder(dest_path_obj, MAILDIR_PATH)
            folder_change = (
                dest_folder if dest_folder != _derive_folder(Path(src_path), MAILDIR_PATH) else None
            )
            if self.reconciler is not None:
                try:
                    self.reconciler.handle_moved(src_path, dest_path, folder=folder_change)
                except Exception as e:
                    log.error("reconciler on_moved failed: %s", e)
            else:
                # Default deployment has no reconciler; still move the
                # indexed_files / message_thread_map filepath forward so
                # future lookups find the current on-disk name.
                try:
                    self.db.update_filepath(src_path, dest_path, folder=folder_change)
                except Exception as e:
                    log.error("update_filepath failed on rename: %s", e)
            return

        # Case 2: new delivery — enqueue for the worker. A flag rename
        # of an already-reaped file also lands here (its source is no
        # longer indexed) and must not be mistaken for new mail.
        if (
            dest_path_obj.parent.name in ("cur", "new")
            and not self.db.is_indexed(dest_path)
            and not self._is_reaped_or_deleted(dest_path)
        ):
            self.queue.enqueue(dest_path, REASON_ON_MOVED)


# Emit a p50/p95/max timing summary roughly once per this many processed
# messages. The aggregator itself has an independent rolling window —
# this constant only controls how often the line is logged, not how many
# samples back the percentiles look. It is unrelated to the health-file
# heartbeat, which is refreshed per message, around each embed call and
# after each attachment page read.
TIMING_LOG_EVERY = 25


def _iter_maildir_messages(root: Path):
    """Yield every message file under ``root`` whose parent is ``cur`` or
    ``new``, at any nesting depth. mbsync ``SubFolders Verbatim`` can
    produce ``Clients/ABC/cur/msg`` — a flat ``iterdir`` over ``root``
    would miss every nested folder's mail."""
    for filepath in root.rglob("*"):
        if filepath.is_file() and filepath.parent.name in ("cur", "new"):
            yield filepath


# Exception types whose text cannot carry mail content, so
# ``_stage_error`` keeps it. Kept deliberately small: every other type
# (email parser/generator errors, codec and charset-lookup errors,
# ``ValueError``, ``sqlite3.Error``, library errors) is reduced to its
# name, which costs debuggability but cannot leak a message.
# ``OSError`` is not listed: any library can raise it with free text,
# so ``_stage_error`` renders it from its errno alone.
_STAGE_ERROR_KEEP_TEXT: tuple[type[BaseException], ...] = (
    # Path and byte sizes only (parser.py).
    OversizedMessageError,
    # Fixed text plus counts by contract (embedder.py).
    EmbedResponseError,
)


def _stage_error(exc: BaseException) -> str:
    """Render a pipeline-stage exception for ``indexing_jobs.last_error``
    and the queue's retry / dead-letter log lines.

    Only types in ``_STAGE_ERROR_KEEP_TEXT`` keep their message; the
    rest are rendered as the type name alone, because their text can
    quote the message being indexed (``HeaderWriteError`` embeds the
    refused header, a codec error its data, a charset ``LookupError``
    the sender's label, and an FTS5 ``sqlite3.OperationalError`` its
    query term). Uses ``str()``, never ``repr()``: some exceptions carry
    their input as an attribute that only ``repr()`` shows.
    """
    if isinstance(exc, _STAGE_ERROR_KEEP_TEXT):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        # The errno's fixed ``os.strerror`` text, never the exception's
        # own message or filename; the job row already holds the path.
        # ``os.strerror`` raises outside the C int range.
        try:
            reason = os.strerror(exc.errno)
        except OverflowError, ValueError:
            return type(exc).__name__
        return f"{type(exc).__name__}: [Errno {exc.errno}] {reason}"
    return type(exc).__name__


@dataclass
class _BatchedMsg:
    """Per-message state carried through the two-phase batched indexer.

    Phase 1 populates ``msg`` and ``thread`` and commits the thread
    membership with a seed thread vector chosen from a three-case
    priority chain: ``mean(existing chunk vectors)`` when the thread
    is already indexed with content; the prior ``threads_vec`` row
    when the thread is chunkless but has a non-zero embedding (covers
    subject-fallback threads); otherwise placeholder zero (a new
    thread, or a chunkless one whose stored vector is still zero after
    a crash between phases). Phase 2a populates the chunk + attachment-plan
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


# Unreadable-file handoff (see ``_phase1_commit_thread``): retry every
# minute, for up to a day after the job was enqueued.
PERMISSION_DEFER_SECS = 60
# How long a job for a T-flagged, still-indexed file is parked while
# deletion reconciliation decides its message (see ``_drain_queue_batched``).
TRASHED_DEFER_SECS = 60 * 60
PERMISSION_DEFER_WINDOW_SECS = 24 * 60 * 60


def _enqueued_within(row: sqlite3.Row, seconds: int) -> bool:
    created = datetime.fromisoformat(row["created_at"])
    return (datetime.now(UTC) - created).total_seconds() < seconds


def _phase1_commit_thread(
    row: sqlite3.Row,
    db: Database,
    threader: Threader,
    queue: IndexingQueue,
) -> _BatchedMsg | None:
    """Phase 1 of the batched indexer for one message.

    Parse → thread → ``upsert_thread`` with a seed vector chosen from
    a three-case priority chain: ``mean`` of the thread's existing
    chunk vectors when it has any; the prior non-zero ``threads_vec``
    row when the thread is chunkless (subject-fallback threads);
    otherwise the placeholder zero, for a new thread or a chunkless one
    whose stored vector is still zero (a retry after a crash between
    phases). Returns a populated ``_BatchedMsg`` on success. On any
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
    except PermissionError as e:
        # mbsync ``chmod go+r``s new files only after its whole sync
        # finishes, so during a long sync a delivered file is still 0600
        # to this UID for longer than the retry budget. That is the
        # expected handoff, not the message's fault: defer without an
        # attempt. A fault that outlasts any sync falls through to the
        # normal retry path so it still ends in a visible dead row.
        if _enqueued_within(row, PERMISSION_DEFER_WINDOW_SECS):
            queue.defer(
                filepath,
                stage="parse",
                error=_stage_error(e),
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=PERMISSION_DEFER_SECS,
            )
        else:
            queue.mark_failed(filepath, stage="parse", error=_stage_error(e))
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
        queue.mark_failed(filepath, stage="parse", error=_stage_error(e))
        return None
    parse_ms = (time.perf_counter() - t0) * 1000
    if msg is None:
        # Parser returned None for a terminal reason (no Message-ID).
        # Dead-letter rather than delete the row: the file is never
        # written to ``indexed_files``, so a deleted row would let every
        # Maildir walk re-enqueue and re-parse it forever. The dead row
        # makes the walk skip it and keeps it visible in queue stats.
        queue.mark_dead_terminal(filepath, stage="parse", error="unindexable: no Message-ID")
        return None

    t0 = time.perf_counter()
    try:
        # Before threading, so the thread range, messages row and chunk
        # dates all see the same date.
        db.keep_persisted_fallback_date(msg)
        thread = threader.assign_thread(msg)
    except Exception as e:
        queue.mark_failed(filepath, stage="thread", error=_stage_error(e))
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
        queue.mark_failed(filepath, stage="thread_commit", error=_stage_error(e))
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


def _chunk_embed_input(subject_line: str, chunk_text: str) -> str:
    """Embedding input for a reply's first body chunk: ``subject_line``
    then the chunk text, within ``CHUNK_MAX_TOKENS`` (#439).

    The subject is attacker-controlled and unbounded, so it is cut to
    the tokens the chunk leaves free. When the chunk is already at the
    ceiling the prefix is dropped and the chunk is embedded as stored.
    The subject is first cut to 16 characters per budget token, so a
    huge subject is never tokenized whole; real tokens are shorter than
    that, so the cut does not shorten what fits.
    """
    # Two tokens of slack for the "\n\n" separator and any merge at
    # the join; the final count below is the actual guard.
    budget = CHUNK_MAX_TOKENS - estimate_tokens(chunk_text) - 2
    if budget <= 0:
        return chunk_text
    prefix = truncate_to_tokens(subject_line[: budget * 16], budget)
    if not prefix.strip():
        return chunk_text
    combined = f"{prefix}\n\n{chunk_text}"
    if estimate_tokens(combined) > CHUNK_MAX_TOKENS:
        return chunk_text
    return combined


def _phase2a_collect_chunks(
    state: _BatchedMsg,
    db: Database,
    all_texts: list[str],
    *,
    progress: Callable[[], None] = lambda: None,
    batch_extractions: dict[str, ExtractionResult] | None = None,
) -> tuple[bool, str | None]:
    """Phase 2a: chunk the body and attachments WITHOUT embedding.

    Appends every new chunk's text to the shared ``all_texts`` list and
    records the offsets on ``state`` so Phase 2c can read its vectors
    back. ``batch_extractions`` carries the batch's uncommitted
    extraction results, so identical bytes are extracted once per batch
    (#237). Returns ``(True, None)`` on success or ``(False, error)`` on
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
    the message has no new chunks AND either the thread has no
    committed chunks at the moment of the check or this message's
    Phase 2c will delete chunks it committed earlier (which may be the
    thread's last ones).
    """
    msg = state.msg
    t0 = time.perf_counter()
    try:
        body_chunks = chunk_message(
            message_pk=msg.claimant_id,
            body_text=strip_for_embedding(msg.body_text or ""),
            target_tokens=CHUNK_TARGET_TOKENS,
            max_tokens=CHUNK_MAX_TOKENS,
            overlap_tokens=CHUNK_OVERLAP_TOKENS,
        )
        stored_ids = db.get_chunk_ids_for_message(msg.claimant_id)
        new_body = [c for c in body_chunks if c.chunk_id not in stored_ids]
        # Whether Phase 2c will delete chunks this message committed
        # earlier without adding any (a body or a re-extracted
        # attachment that now yields no text). The thread may then end
        # up chunkless with a vector still seeded from the deleted
        # chunks, so the fallback below is reserved for that case too.
        clears_chunks = bool(stored_ids) and not body_chunks
        new_body_offsets: list[int] = []
        # A reply that changed the subject carries it into the embedding
        # input of its first body chunk only (#303). The stored chunk
        # text, offsets and ID stay body-only, since chunks are the
        # authoritative body store; keyword search gets the subject from
        # the thread's FTS subject column instead.
        subject_line = reply_subject_line(msg, state.thread.subject)
        for c in new_body:
            new_body_offsets.append(len(all_texts))
            if subject_line and c.chunk_index == 0:
                all_texts.append(_chunk_embed_input(subject_line, c.text))
            else:
                all_texts.append(c.text)

        attach_plans: list[AttachmentWritePlan] = []
        attach_new_chunks: list[list[MessageChunk]] = []
        attach_offsets: list[list[int]] = []
        # Copies of the same bytes in one message chunk to the same chunk
        # IDs (the chunk key is message + content hash): embed each once.
        queued_attach_offsets: dict[str, int] = {}
        attach_stored_ids: list[set[str]] = []
        if INDEXER_ATTACHMENT_EXTRACTION_ENABLED and msg.attachments:
            cap = (
                INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS
                if INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS > 0
                else None
            )
            for occurrence_index, attachment in enumerate(msg.attachments):
                # Each attachment's extraction is separately bounded
                # (byte caps, OCR page cap and timeouts), so it is the
                # unit the stall guard's limit applies to.
                progress()
                # ``embedder=None`` defers the embed step — the plan
                # comes back with empty embeddings_by_chunk_id and
                # Phase 2c populates it from the batched embed result.
                plan = prepare_attachment_writes(
                    attachment=attachment,
                    claimant_id=msg.claimant_id,
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
                    batch_extractions=batch_extractions,
                    # Each page read refreshes the heartbeat, so a long
                    # OCR does not read as unhealthy (#485). It does not
                    # restart the stall guard's clock, which stays per
                    # attachment.
                    on_progress=_extraction_heartbeat,
                )
                stored_attach_ids = db.get_chunk_ids_for_message(
                    msg.claimant_id, attachment_id=attachment.content_hash
                )
                plan_new = [c for c in plan.chunks if c.chunk_id not in stored_attach_ids]
                plan_offsets: list[int] = []
                for c in plan_new:
                    if c.chunk_id not in queued_attach_offsets:
                        queued_attach_offsets[c.chunk_id] = len(all_texts)
                        all_texts.append(c.text)
                    plan_offsets.append(queued_attach_offsets[c.chunk_id])
                attach_plans.append(plan)
                attach_new_chunks.append(plan_new)
                attach_offsets.append(plan_offsets)
                attach_stored_ids.append(stored_attach_ids)
        # A plan without text clears its attachment's chunk slice in
        # Phase 2c, unless another copy of the same bytes in this message
        # fills it: that copy counted the stored chunks as kept and
        # embedded none of them, so it could not restore a cleared slice.
        filled = {plan.attachment.content_hash for plan in attach_plans if plan.chunks}
        for plan, stored_attach_ids in zip(attach_plans, attach_stored_ids):
            if plan.chunks:
                continue
            if plan.attachment.content_hash in filled:
                plan.clears_stale_chunks = False
            else:
                clears_chunks = clears_chunks or bool(stored_attach_ids)
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
        if not has_new_chunks and (
            clears_chunks or not db.thread_has_chunks(state.thread.thread_id)
        ):
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
            # through to the message's own subject while display_subject
            # is NULL.
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
        return False, _stage_error(e)

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
    # see ``replace_message_chunks``.
    msg_date_iso = msg.date.isoformat()
    try:
        with db.transaction():
            db.replace_message_chunks(
                claimant_id=msg.claimant_id,
                thread_id=thread.thread_id,
                chunks=state.body_chunks,
                embeddings_by_chunk_id=body_embs,
                message_date=msg_date_iso,
            )
            for plan in state.attach_plans:
                apply_attachment_writes(
                    plan=plan,
                    claimant_id=msg.claimant_id,
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
        return False, _stage_error(e)
    return True, None


class _EmbedOutageBreaker:
    """Pauses queue draining while the embedder is unavailable.

    Without it, every due batch during an outage would hit the dead
    embedder in turn (each call spending tenacity's in-call retries)
    and churn the whole queue. When a batch fails AND a probe confirms
    the embedder itself is down, the breaker opens: draining stops
    until the backoff elapses, then one batch tests the embedder again.
    The backoff doubles per consecutive outage up to ``cap_seconds``
    and resets on the first successful embed.

    Pausing also skips Phase 1, so mail arriving during an outage is
    not keyword-searchable until the embedder returns. That is the
    price of not hammering a down provider; the queue keeps every job.
    """

    def __init__(self, base_seconds: float = 30, cap_seconds: float = 600):
        self.base_seconds = base_seconds
        self.cap_seconds = cap_seconds
        self.consecutive_failures = 0
        self.open_until = 0.0

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def record_failure(self, now: float) -> float:
        """Open the breaker; returns the pause length in seconds."""
        self.consecutive_failures += 1
        delay = min(
            self.base_seconds * (2 ** (self.consecutive_failures - 1)),
            self.cap_seconds,
        )
        self.open_until = now + delay
        return delay

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0


# Deferral delay for an outage when no breaker is supplied (compat
# shims and tests). Production always passes the main-loop breaker.
_OUTAGE_DEFER_SECONDS = 30.0

_EMBED_PROBE_TEXT = "embedder health probe"


def _probe_embedder(embedder: EmbeddingBackend) -> BaseException | None:
    """Embed a tiny known-good input. Returns the error if the embedder
    cannot embed anything (outage or misconfiguration), else ``None``."""
    # The failure that prompted this probe may have run a full retry
    # cycle; refresh the heartbeat so the probe's own cycle starts fresh.
    touch_health_file()
    try:
        embedder.embed(_EMBED_PROBE_TEXT)
    except Exception as e:
        return e
    return None


def _entry_text_offsets(entry: _BatchedMsg) -> list[int]:
    """Every index into the batch's flat embed-input list that belongs
    to ``entry`` — body chunks, attachment chunks, subject fallback —
    each once (copies of one attachment share their chunks' offsets)."""
    offsets = list(entry.new_body_offsets)
    for plan_offsets in entry.attach_offsets:
        offsets.extend(plan_offsets)
    if entry.subject_fallback_offset is not None:
        offsets.append(entry.subject_fallback_offset)
    return list(dict.fromkeys(offsets))


def _pause_embedding(
    entries: list[_BatchedMsg],
    queue: IndexingQueue,
    breaker: _EmbedOutageBreaker | None,
    exc: BaseException,
) -> None:
    """The embedder itself is failing: defer ``entries`` without
    spending attempts and open the breaker so draining stops.

    A configuration error (or a probe the provider refuses outright)
    records ``operator_action_required``; anything else is an outage
    and records ``retryable``.
    """
    kind = classify_embed_failure(exc)
    error_class = (
        ERROR_CLASS_OPERATOR
        if kind in (EMBED_FAILURE_CONFIGURATION, EMBED_FAILURE_REJECTED_INPUT)
        else ERROR_CLASS_RETRYABLE
    )
    err_repr = scrub_embed_error(exc)
    delay = (
        breaker.record_failure(time.monotonic()) if breaker is not None else _OUTAGE_DEFER_SECONDS
    )
    for entry in entries:
        queue.defer(
            entry.row["filepath"],
            stage="embed",
            error=err_repr,
            error_class=error_class,
            delay_seconds=delay,
        )
    if error_class == ERROR_CLASS_OPERATOR:
        log.error(
            "embedder rejected credentials or model (%s) — check EMBED_BASE_URL, "
            "EMBED_MODEL and the embed API key. Deferred %d message(s); "
            "indexing paused %ds.",
            err_repr,
            len(entries),
            delay,
        )
    else:
        log.error(
            "embedder unavailable (%s). Deferred %d message(s) without "
            "spending retry attempts; indexing paused %ds.",
            err_repr,
            len(entries),
            delay,
        )


def _embed_progress(queue: IndexingQueue) -> Callable[[], None]:
    """Callback for each completed embed request: refresh the heartbeat
    and restart the stall guard's clock. A lone survivor stays charged,
    and watched, through the bulk embed; each request is separately
    bounded, so it is the unit the guard's limit applies to."""

    def progress() -> None:
        touch_health_file()
        queue.note_progress()

    return progress


def _embed_message_texts(
    texts: list[str],
    embedder: EmbeddingBackend,
    on_progress: Callable[[], None] | None = None,
) -> list[list[float]]:
    """Embed one message's texts, telling a request limit from bad content.

    ``embed_batch`` packs up to ``EMBED_BATCH_SIZE`` texts into each HTTP
    request. A provider or gateway that rejects such a request (413, or
    a 400 / 422 batch limit) has only shown that the *request* was
    unacceptable, not that any of the message's content is. So a
    rejected multi-input request is retried one text per request; only
    a rejection of a single text propagates as evidence against the
    source. Every other failure propagates unchanged for the caller to
    attribute.
    """
    if on_progress is None:
        on_progress = touch_health_file
    try:
        return embedder.embed_batch(texts, on_batch_complete=on_progress)
    except Exception as e:
        if len(texts) <= 1 or classify_embed_failure(e) != EMBED_FAILURE_REJECTED_INPUT:
            raise
        log.warning(
            "embed request with %d inputs rejected (%s); retrying one input per "
            "request. If this recurs, the provider's request limit is below "
            "EMBED_BATCH_SIZE — lower it.",
            len(texts),
            scrub_embed_error(e),
        )
    return [embedder.embed_batch([t], on_batch_complete=on_progress)[0] for t in texts]


def _embed_each_message(
    survivors: list[_BatchedMsg],
    all_texts: list[str],
    embedder: EmbeddingBackend,
    queue: IndexingQueue,
    breaker: _EmbedOutageBreaker | None,
) -> tuple[list[list[float]], list[_BatchedMsg], bool]:
    """Embed each message's texts on its own after a failed batch embed.

    Called once a probe has shown the embedder can embed *something*.
    That probe describes one tiny request, not the provider's state for
    every later request, so each individual failure is attributed with
    ``classify_embed_failure`` before any message is charged:

    * ``rejected_input`` (400 / 413 / 422) on a single text — the
      provider refused this message's content: ``mark_dead_terminal``
      as a permanent source failure. The only terminal outcome. (A
      rejected multi-text request is first split into one text per
      request by ``_embed_message_texts``, so a request-size limit is
      never charged to the source.)
    * ``infrastructure`` / ``configuration`` (transport, 408, 429,
      401 / 403 / 404) — not the message's fault: this and every
      remaining message are deferred without spending attempts, the
      breaker opens, and isolation stops.
    * ``uncertain`` (5xx, integrity errors) — probe again. A failing
      probe means the provider went down: pause as above. A passing
      probe points at this input: ``mark_failed`` spends one attempt,
      so a genuinely poison input still dead-letters eventually without
      stalling the queue behind the breaker.

    Returns a vector list aligned to ``all_texts`` (slots of failed
    messages stay empty and are never read), the entries that embedded
    cleanly, and whether embedding was paused.
    """
    vectors: list[list[float]] = [[] for _ in all_texts]
    ok: list[_BatchedMsg] = []
    for index, entry in enumerate(survivors):
        touch_health_file()
        offsets = _entry_text_offsets(entry)
        filepath = entry.row["filepath"]
        try:
            entry_vectors = (
                _embed_message_texts(
                    [all_texts[i] for i in offsets], embedder, _embed_progress(queue)
                )
                if offsets
                else []
            )
        except Exception as e:
            failure: BaseException = e
            kind = classify_embed_failure(e)
            if kind == EMBED_FAILURE_UNCERTAIN:
                probe_error = _probe_embedder(embedder)
                if probe_error is None:
                    queue.mark_failed(filepath, stage="embed", error=scrub_embed_error(e))
                    continue
                failure = probe_error
            elif kind == EMBED_FAILURE_REJECTED_INPUT:
                queue.mark_dead_terminal(filepath, stage="embed", error=scrub_embed_error(e))
                continue
            _pause_embedding(survivors[index:], queue, breaker, failure)
            return vectors, ok, True
        for i, vector in zip(offsets, entry_vectors):
            vectors[i] = vector
        ok.append(entry)
        touch_health_file()
    return vectors, ok, False


def _drain_queue_batched(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
    *,
    batch_size: int,
    timing_aggregator: TimingAggregator,
    max_passes: int | None = None,
    breaker: _EmbedOutageBreaker | None = None,
    skip_trashed: bool = False,
) -> int:
    """Drain the queue in two-phase batches.

    Phase 1 commits thread membership per-message with a seed thread
    vector — ``mean(existing chunk vectors)`` for threads with chunks,
    the prior non-zero ``threads_vec`` row for chunkless ones,
    placeholder zero for new ones and for chunkless ones whose stored
    vector is still zero after a crash — so (a) the next message
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

    ``skip_trashed`` (set whenever deletion reconciliation is enabled;
    see ``_enqueue_unindexed_messages``) never indexes a claimed job
    whose file is T-flagged: the reconciler owns that message now, and
    indexing it would outlive its reap. The job of a message still in
    the index is parked without spending an attempt: the reap deletes it
    with the message's rows, or, if mbsync clears the flag first, the
    rename moves it to the live path where it runs. A trashed file that
    was never indexed has nothing to keep, so its job is dropped.

    Failure isolation:

    * Phase 1 error for one message — that message marked failed,
      others continue.
    * Phase 2a (chunk/extract) error for one message — marked failed,
      Phase 1's commit stays. Vector-less but text-searchable.
    * Phase 2b (embed) error — a probe decides whose fault it is.
      Embedder down or misconfigured: every in-flight row is deferred
      without spending attempts, ``breaker`` opens, and this call
      stops draining. Embedder healthy: each message is re-embedded on
      its own, so one bad input cannot fail its batchmates; each
      individual failure is attributed before anyone is charged (see
      ``_embed_each_message``). Phase 1 commits remain either way; a
      later pass re-runs Phase 1 (idempotent upsert) plus Phase 2.
    * Phase 2c (DB write) error for one message — marked failed,
      others succeed.
    """
    processed = 0
    passes = 0
    while True:
        if max_passes is not None and passes >= max_passes:
            break
        if breaker is not None and not breaker.allow(time.monotonic()):
            break
        passes += 1
        # ---- Gather batch + Phase 1 ----
        # Snapshot up to batch_size distinct queued rows in one query
        # so the gather loop cannot re-claim the same row repeatedly
        # while we defer mark_succeeded to Phase 2c.
        rows = queue.claim_batch(batch_size)
        if not rows:
            break
        # A row still marked ``interrupted`` was mid-step when the
        # indexer died. An out-of-memory kill can come from the whole
        # batch's footprint rather than that message, and a restart
        # replays the same batch in the same order, so the row runs
        # alone first: only a message that fails on its own keeps
        # accumulating charges toward ``dead``.
        interrupted = [row for row in rows if row["last_stage"] == INTERRUPTED_STAGE]
        if interrupted and len(rows) > 1:
            rows = interrupted[:1]
        batch: list[_BatchedMsg] = []
        for row in rows:
            if skip_trashed and is_trashed(row["filepath"]):
                if db.find_message_entry_by_filepath(row["filepath"]) is None:
                    queue.mark_skipped(row["filepath"], reason="trashed")
                else:
                    queue.defer(
                        row["filepath"],
                        stage="trashed",
                        error="file is T-flagged; parked until reaped or restored",
                        error_class=ERROR_CLASS_RETRYABLE,
                        delay_seconds=TRASHED_DEFER_SECS,
                    )
                continue
            # Parse and extraction are the steps hostile input can crash
            # or hang, so each runs with its message charged one attempt
            # (see ``IndexingQueue.begin_attempt``). The refund is not in
            # a ``finally``: an exception escaping the step takes the
            # process down, and the charge must survive that.
            if not queue.begin_attempt(row["filepath"]):
                continue
            entry = _phase1_commit_thread(row, db, threader, queue)
            queue.end_attempt(row["filepath"])
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
        batch_extractions: dict[str, ExtractionResult] = {}
        for entry in batch:
            if not queue.begin_attempt(entry.row["filepath"]):
                continue
            ok, err = _phase2a_collect_chunks(
                entry,
                db,
                all_texts,
                progress=queue.note_progress,
                batch_extractions=batch_extractions,
            )
            queue.end_attempt(entry.row["filepath"])
            if ok:
                survivors.append(entry)
            else:
                queue.mark_failed(entry.row["filepath"], stage="chunk", error=err or "")
            touch_health_file()

        if not survivors:
            continue

        # The bulk embed and the vector commits hold the whole batch's
        # vectors, so a kill there cannot be pinned on one message.
        # Several survivors are marked ``interrupted`` without a charge,
        # which replays each alone after a restart; a lone survivor —
        # already running alone — stays charged until its outcome is
        # recorded, so one that dies even alone still reaches ``dead``.
        if len(survivors) == 1:
            if not queue.begin_attempt(survivors[0].row["filepath"]):
                continue
        else:
            queue.mark_interrupted([entry.row["filepath"] for entry in survivors])

        # Refresh the heartbeat just before the bulk embed so a slow
        # cloud-embedder round-trip (potentially tens of seconds for a
        # full batch of chunks) doesn't age the health file past
        # HEALTH_MAX_AGE_SECONDS while no per-message touch fires.
        touch_health_file()

        # ---- Phase 2b: bulk embed across batch ----
        t_embed_start = time.perf_counter()
        paused = False
        try:
            vectors = (
                embedder.embed_batch(all_texts, on_batch_complete=_embed_progress(queue))
                if all_texts
                else []
            )
        except Exception as e:
            # Scrub the error before persistence: ``APIStatusError`` can
            # echo input fragments (email body text) on 4xx, and
            # ``last_error`` rides into ``indexing_jobs.last_error`` +
            # operator log sinks. ``scrub_embed_error`` keeps full repr
            # for safe error shapes (connection / timeout / our own
            # ``EmbedResponseError``), trims SDK status errors to
            # type + status_code, and anything else to its type.
            err_repr = scrub_embed_error(e)
            probe_error = _probe_embedder(embedder)
            if probe_error is not None:
                _pause_embedding(survivors, queue, breaker, probe_error)
                break
            log.warning(
                "batched embed failed (%s) but the embedder is healthy; "
                "embedding %d message(s) individually to isolate the bad input.",
                err_repr,
                len(survivors),
            )
            vectors, survivors, paused = _embed_each_message(
                survivors, all_texts, embedder, queue, breaker
            )
        if breaker is not None and not paused:
            breaker.record_success()
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

        if paused:
            # Messages embedded before the pause were committed above;
            # the rest are deferred and the breaker is open.
            break

        if processed and processed % TIMING_LOG_EVERY < batch_size:
            line = format_summary(timing_aggregator.summary())
            if line:
                log.info(line)

    return processed


def _recover_zero_vector_threads(
    db: Database,
    queue: IndexingQueue,
    *,
    resurrect_dead: bool = False,
    skip_trashed: bool = False,
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
      runs ``make requeue-dead`` (``src/requeue_dead.py``), which
      resets dead rows to ``queued`` with a fresh attempt budget.

      Note: simply touching the Maildir file does NOT re-enqueue
      it. The watchdog handles ``on_created`` and ``on_moved``
      events but not ``on_modified``, and ``initial_index``
      consults ``queue.is_dead`` and skips dead-lettered paths
      regardless of file activity.

      ``resurrect_dead=True`` re-enqueues dead rows among this
      sweep's candidates; no production call site uses it.

    Skips files that already have a 'queued' row (active retry
    cascade in flight; clobbering its row would reset the attempts
    counter mid-cascade), and with ``skip_trashed`` (deletion
    reconciliation enabled) T-flagged files, which the reaper owns.

    Returns the number of files re-enqueued for visibility in logs.
    """
    candidates = db.find_zero_vector_chunkless_thread_filepaths()
    if not candidates:
        return 0

    re_enqueued = 0
    skipped_pending = 0
    skipped_dead = 0
    for filepath in candidates:
        if skip_trashed and is_trashed(filepath):
            continue
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


def _requeue_stale_extractions(db: Database, queue: IndexingQueue) -> int:
    """Re-queue messages whose cached attachment extraction came from an
    older version of an extractor (see ``extractors.EXTRACTOR_VERSIONS``),
    and, while OCR is on, messages carrying an attachment cached as "OCR
    disabled" (an image, or a PDF without a digital text layer, skipped
    while OCR was off) whose reprocess would run OCR on it (#300).

    Reprocessing re-runs that extractor (a stale row is refreshed from
    any occurrence of its bytes) and replaces the attachment's chunks,
    and the row is rewritten with the current version, so each message
    is re-queued once. Every message carrying the bytes is included, not
    only those whose filename or MIME type resolves to the extractor:
    they all indexed the shared stale text. An "OCR disabled" row has no
    extractor to refresh it from another occurrence, so only messages
    whose own occurrence re-runs extraction are re-queued; the re-run
    replaces the row, which keeps that once-only too. Like the zero-vector
    recovery sweep, files already queued or dead-lettered are left
    alone. Skipped entirely when attachment extraction is disabled,
    since the drain would not re-stamp the rows. Returns the number of
    files re-queued.
    """
    if not INDEXER_ATTACHMENT_EXTRACTION_ENABLED:
        return 0
    stale = [
        name
        for name in db.get_extractor_names()
        if is_stale_extractor(name, ocr_enabled=INDEXER_OCR_ENABLED)
    ]
    filepaths = set(db.find_filepaths_with_extractors(stale))
    if INDEXER_OCR_ENABLED:
        filepaths.update(
            row["filepath"]
            for row in db.find_ocr_disabled_attachments()
            if reruns_once_ocr_is_on(row["extraction_error"], row["content_type"], row["filename"])
        )
    re_enqueued = 0
    for filepath in sorted(filepaths):
        if queue.has_pending_row(filepath) or queue.is_dead(filepath):
            continue
        queue.enqueue(filepath, REASON_REEXTRACT)
        re_enqueued += 1
    if re_enqueued:
        log.info(
            "re-queued %d message(s) whose attachments were extracted by an older "
            "extractor version (%s) or skipped while OCR was off.",
            re_enqueued,
            ", ".join(sorted(stale)) or "none",
        )
    return re_enqueued


def _enqueue_unindexed_messages(
    db: Database,
    queue: IndexingQueue,
    root: Path,
    reason: str,
    *,
    skip_trashed: bool = False,
) -> int:
    """Walk the Maildir and enqueue every message not yet indexed.

    Shared by the startup scan and the periodic rescan. The watchdog is
    the low-latency path; this walk is the eventual-completeness
    backstop for any file whose event was missed (a restart, event
    coalescing, a delivery while the observer was not running).

    Skips files that are already indexed, dead-lettered, or already
    queued. ``enqueue`` is ``INSERT OR REPLACE``, so re-enqueueing a
    queued row would reset an in-flight retry cascade to zero attempts
    — repeated walks could then retry a failing file forever without
    it ever reaching the dead-letter state. Dead rows are left for the
    operator: the walk only proves the file exists on disk, not that
    anything about it changed since the last failure. (Watchdog
    ``on_created`` / ``on_moved`` events still go through ``enqueue``
    and DO reset prior state, because those signal a real change.)

    ``skip_trashed`` must be True whenever deletion reconciliation is
    enabled. A T-flagged file is then deleted upstream: after the
    reaper removes it from the index (``unlink_on_reap=False`` keeps the
    .eml on disk) it is unindexed and unqueued, and enqueueing it would
    resurrect the message into search. With reconciliation disabled the
    index is append-only and trashed files are indexed like any other.

    Returns the number of files enqueued.
    """
    enqueued = 0
    skipped_dead = 0
    for filepath in _iter_maildir_messages(root):
        path_str = str(filepath)
        if db.is_indexed(path_str):
            continue
        if skip_trashed and is_trashed(filepath):
            continue
        if queue.is_dead(path_str):
            skipped_dead += 1
            continue
        if queue.has_pending_row(path_str):
            continue
        queue.enqueue(path_str, reason)
        enqueued += 1
    if enqueued or skipped_dead:
        log.info(
            "Maildir walk (%s): enqueued %d message(s), skipped %d dead-lettered.",
            reason,
            enqueued,
            skipped_dead,
        )
    return enqueued


def _refresh_folder_watches(
    folder_watches: FolderWatchRefresher,
    db: Database,
    queue: IndexingQueue,
    *,
    skip_trashed: bool = False,
) -> bool:
    """After an mbsync sync, watch the folders it made readable (#516).

    mbsync creates folders 0700 and opens them to the indexer only in
    its post-sync permission repair, which the stamp follows, so the
    watch cannot have been added to a folder created during the sync.
    When ``folder_watches`` re-schedules the watch, the old watch is
    closed before the new one walks the tree, and events in that gap
    are lost: heal renames, then queue every unindexed message, as at
    startup. That walk also queues mail already delivered into the
    newly watched folders. Returns whether the watch was re-scheduled.
    """
    if not folder_watches.refresh():
        return False
    sweep_paths(db)
    _enqueue_unindexed_messages(db, queue, MAILDIR_PATH, REASON_RESCAN, skip_trashed=skip_trashed)
    return True


def initial_index(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
    *,
    skip_trashed: bool = False,
    breaker: _EmbedOutageBreaker | None = None,
    ingestion_state: _IngestionStateRecorder | None = None,
):
    """Enqueue every unindexed Maildir message and drain the queue.

    ``skip_trashed``: see ``_enqueue_unindexed_messages``. ``breaker``
    is shared with the main loop so an embedder outage that starts
    during the initial drain carries its backoff into steady state
    (the drain returns early while the breaker is open; the main loop
    finishes the queue once the embedder is back). ``ingestion_state``
    acknowledges the sync stamp read before the walk once the walk has
    queued everything that sync delivered.

    Refreshes the health file after every processed message so that
    long initial indexes (large mailboxes, slow embedding service, OCR
    on scanned PDFs) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the
    healthcheck and cause the container to be reported unhealthy
    mid-scan. Within one message the heartbeat is also refreshed after
    each attachment page read and each embed request, so only a single
    step that stalls past ``HEALTH_MAX_AGE_SECONDS`` trips the
    healthcheck.

    Routing the initial scan through the queue — rather than indexing
    files inline — means a crash or embedding service outage mid-scan leaves the
    untouched work durably queued instead of dropped. The next restart
    resumes from ``indexing_jobs`` rather than rescanning the whole
    Maildir and relying on ``is_indexed`` to filter.
    """
    log.info("Running initial index scan...")
    # Dead-lettered files are not resurrected on routine startup:
    # without that skip, every container restart re-ran the same
    # 5-attempt × 30s backoff cascade against the same poison-pill
    # payloads — observed to add up to ~30 minutes of wasted embedding
    # service load per dead file per restart.
    stamp = ingestion_state.read_stamp() if ingestion_state is not None else None
    _enqueue_unindexed_messages(
        db, queue, MAILDIR_PATH, REASON_INITIAL_SCAN, skip_trashed=skip_trashed
    )
    if ingestion_state is not None:
        ingestion_state.acknowledge(stamp)

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
    _recover_zero_vector_threads(db, queue, skip_trashed=skip_trashed)
    # Messages whose attachments an extractor fix would now read
    # differently; re-queued once per version bump.
    _requeue_stale_extractions(db, queue)

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
        breaker=breaker,
        skip_trashed=skip_trashed,
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


def _load_authority_rules(path: Path) -> AuthorityRules:
    """Load the operator rules file, failing closed on a malformed one.

    The error names the file position, never a pattern (the file holds
    addresses), so it is safe to log.
    """
    try:
        rules = load_authority_rules(path)
    except AuthorityRulesError as exc:
        raise SystemExit(f"Invalid source-authority rules: {exc}") from None
    if rules.pattern_count:
        log.info(
            "Authority rules: %d address and %d domain pattern(s) from %s",
            len(rules.addresses),
            len(rules.domains),
            path,
        )
    else:
        log.info("Authority rules: none at %s; every entity is unclassified", path)
    return rules


def _log_reconciler_config(cfg: ReconcilerConfig) -> None:
    if not cfg.enabled:
        log.info(
            "Deletion reconciliation: disabled (archive mode; "
            "unset INDEXER_DELETION_ENABLED to mirror upstream deletions)"
        )
        return
    log.info(
        "Deletion reconciliation: enabled "
        "(mirror mode; grace=%dd, sweep=%ds, max_batch=%.1f%%, force=%s, unlink=%s)",
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

    # Before opening the database, so a malformed file fails fast.
    authority_rules = _load_authority_rules(AUTHORITY_RULES_PATH)
    db = Database(SQLITE_PATH)
    reclassified = db.set_authority_rules(authority_rules)
    if reclassified:
        log.info("Authority rules: reclassified %d existing entities", reclassified)
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
    if INDEXER_MESSAGE_TIMEOUT_SECONDS:
        StallGuard(queue, limit_seconds=INDEXER_MESSAGE_TIMEOUT_SECONDS).start()
        log.info("Stall guard: exits after %ds on one message", INDEXER_MESSAGE_TIMEOUT_SECONDS)
    else:
        log.info("Stall guard: disabled (INDEXER_MESSAGE_TIMEOUT_SECONDS=0)")
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

    # Start watching BEFORE the initial drain. On a large mailbox the
    # drain runs for hours; mail mbsync delivers in that window would
    # otherwise never be enqueued. Events that land mid-drain are
    # picked up by the same drain-to-empty loop.
    global _ingestion_state
    ingestion_state = _IngestionStateRecorder(db, MAILDIR_PATH)
    _ingestion_state = ingestion_state
    sync_completed = threading.Event()
    directory_created = threading.Event()
    handler = MaildirHandler(
        db,
        queue,
        reconciler=reconciler,
        ingestion_state=ingestion_state,
        sync_completed=sync_completed,
        directory_created=directory_created,
    )
    observer = Observer()
    folder_watches = FolderWatchRefresher(
        MAILDIR_PATH, observer, handler, directory_created=directory_created
    )
    folder_watches.start()
    observer.start()
    log.info("Watching Maildir for new emails...")

    # Always-on startup rename sweep. mbsync renames files in place for
    # any flag change (e.g. seen ``S`` → seen+replied ``SR``) and when
    # promoting from ``new/`` to ``cur/``. Events that land while the
    # indexer is offline would otherwise leave stale filepaths in
    # ``indexed_files``, which makes later lookups on the renamed file
    # miss. sweep_paths() only updates filepath rows; it does not
    # tombstone missing files, so running it unconditionally preserves
    # archive mode (INDEXER_DELETION_ENABLED=false). It runs before the
    # initial walk so a renamed file's new path is already recorded as
    # indexed: otherwise the walk reprocesses every file renamed while
    # the indexer was down as new mail.
    try:
        sweep_paths(db)
    except Exception as e:
        log.error("startup rename sweep failed: %s", e)

    # Index existing emails
    breaker = _EmbedOutageBreaker()
    initial_index(
        db,
        embedder,
        threader,
        queue,
        skip_trashed=reconciler is not None,
        breaker=breaker,
        ingestion_state=ingestion_state,
    )
    touch_health_file()

    # Startup reconciliation sweep — detect tombstones and path renames that
    # landed while the indexer was offline. Safe to run every startup: it only
    # writes to pending_deletions and updates stored filepaths.
    if reconciler is not None:
        try:
            reconciler.sweep()
            reconciler.reap()
        except Exception as e:
            log.error("startup reconciliation failed: %s", e)

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
                    breaker=breaker,
                    skip_trashed=reconciler is not None,
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
                log.error("queue drain failed: %s", _stage_error(e))

            # A sync completed: watch any folder its permission repair
            # made readable. Cleared first, so a sync that completes
            # during the refresh triggers another.
            if sync_completed.is_set():
                sync_completed.clear()
                try:
                    _refresh_folder_watches(
                        folder_watches, db, queue, skip_trashed=reconciler is not None
                    )
                except Exception as e:
                    log.error("Maildir watch refresh failed: %s", type(e).__name__)

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
            # The same cadence re-walks the Maildir so a file whose
            # watchdog event was missed still gets indexed eventually.
            if now - last_recovery_sweep >= RECOVERY_SWEEP_INTERVAL_SECS:
                try:
                    _recover_zero_vector_threads(db, queue, skip_trashed=reconciler is not None)
                except Exception as e:
                    log.error("periodic recovery sweep failed: %s", e)
                try:
                    stamp = ingestion_state.read_stamp()
                    _enqueue_unindexed_messages(
                        db,
                        queue,
                        MAILDIR_PATH,
                        REASON_RESCAN,
                        skip_trashed=reconciler is not None,
                    )
                    ingestion_state.acknowledge(stamp)
                except Exception as e:
                    log.error("periodic Maildir rescan failed: %s", e)
                # Also re-watch here: a failed sync attempt still opens
                # the folders it created but writes no stamp, and a
                # failed re-schedule leaves no watch until the next try.
                try:
                    _refresh_folder_watches(
                        folder_watches, db, queue, skip_trashed=reconciler is not None
                    )
                except Exception as e:
                    log.error("Maildir watch refresh failed: %s", type(e).__name__)
                last_recovery_sweep = now

            # WAL checkpoint: keep the WAL file size bounded over a
            # long-running container. SQLite's automatic checkpoint
            # lets the WAL be reused once checkpointed but never
            # shrinks the file, so an explicit periodic
            # ``wal_checkpoint(TRUNCATE)`` is what reclaims space. It
            # can only complete when no reader holds an open read
            # transaction on the WAL (``busy`` below).
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
